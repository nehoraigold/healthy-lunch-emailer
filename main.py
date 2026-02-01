# ================= IMPORTS =================

import os
import json
import requests
from bs4 import BeautifulSoup
from mailersend import MailerSendClient, EmailBuilder
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
load_dotenv()

# ================= CONFIG =================
NOTIFY_METHODS = os.environ.get("NOTIFY_METHODS", "email,slack").split(",")

# ================= Dietary ================
MAX_CALORIES = int(os.environ.get("MAX_CALORIES") or 850)
MIN_GRAMS_PROTEIN = int(os.environ.get("MIN_GRAMS_PROTEIN") or 25)

# ================= Email ==================
MAILERSEND_API_KEY = os.environ.get("MAILERSEND_API_KEY")
FROM_EMAIL = os.environ.get("FROM_EMAIL")

# Recipients
# An array of objects, each with "name" (string) and "email" (string)
# Example: [{"name":"John Doe","email":"johndoe@example.com"}]
EMAIL_RECIPIENTS = json.loads(os.environ.get("EMAIL_RECIPIENTS"))

# ================= Slack ==================
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL")

# ================= CONSTS =================

MENU_URL = "https://paloaltonetworks.cafebonappetit.com/#lunch"
AJAX_URL = "https://paloaltonetworks.cafebonappetit.com/wp-admin/admin-ajax.php"
MAX_WORKERS = 10
TOP_N = 5

# =========================================

session = requests.Session()

def fetch_menu_page():
    return requests.get(MENU_URL, timeout=15).text


def extract_lunch_items(html):
    soup = BeautifulSoup(html, "html.parser")
    items = []

    # Lunch tab items only
    for item in soup.select('section#lunch .c-tab__content--active [data-id][data-nonce]'):
        name_tag = item.find("button")
        if not name_tag:
            continue

        items.append({
            "id": item["data-id"],
            "nonce": item["data-nonce"],
            "name": name_tag.get_text(strip=True),
        })

    if not items:
        raise RuntimeError("No lunch items found in HTML")
    
    print(f"Found {len(items)} items")
    return items


def fetch_item_nutrition(session, item_id, nonce):
    params = {
        "action": "get_cm_menu_items",
        "item": item_id,
        "nonce": nonce,
    }

    r = session.get(AJAX_URL, params=params, timeout=10)
    r.raise_for_status()
    return r.json()


def _num(nutrition, key, cast=int):
    try:
        value = nutrition.get(key, {}).get("value", "")
        return cast(value) if value != "" else None
    except (ValueError, TypeError):
        return None


def normalize_item(raw, name, id):
    item = raw["items"][id]

    nutrition = item.get("nutrition_details", {})
    ingredients = item.get("ingredient_details", "").lower()

    calories = _num(nutrition, "calories", float)
    protein = _num(nutrition, "proteinContent", float)
    fat = _num(nutrition, "fatContent", float)
    carbs = _num(nutrition, "carbohydrateContent", float)
    serving_size = _num(nutrition, "servingSize", float)
    serving_size_unit = nutrition.get("servingSize").get("unit")

    return {
        "id": id,
        "name": name,
        "calories": calories,
        "protein_g": protein,
        "fat_g": fat,
        "carbs_g": carbs,
        "serving_size": serving_size,
        "serving_size_unit": serving_size_unit,
        "ingredients": ingredients,
        "dairy_free": not any(
            x in ingredients
            for x in ["milk", "cheese", "butter", "cream", "whey", "casein"]
        ),
    }


def allowed(item):
    if item["calories"] > MAX_CALORIES:
        return False
    if item["protein_g"] < MIN_GRAMS_PROTEIN:
        return False
    return True


def score(item):
    return get_protein_score(item) * get_volume_score(item)


def get_protein_score(item):
    # Higher score if more protein per calories
    return (item["protein_g"] * 10) / item["calories"]


def get_volume_score(item):
    # Higher score if more volume per calories
    return (item["serving_size"] * 100 / item['calories'])


def build_title():
    today = datetime.now().strftime("%A, %B %d")
    return f"🥗 Top Healthy Lunch Picks — {today} 🥗"


def build_message(items):
    if not items:
        return "No lunch options met your criteria today."

    lines = [f"Here are today's top {len(items)} healthy lunch picks:", ""]
    for i, item in enumerate(items, 1):
        lines.extend(
            [
                f"{i}. {item['name']} ({item['serving_size']} {item['serving_size_unit']})",
                f"   Calories: {item['calories']}",
                f"   Protein: {item['protein_g']}g",
                f"   Carbs: {item['carbs_g']}g",
                f"   Fat: {item['fat_g']}g",
                "",
            ]
        )

    return "\n".join(lines)


def notify(title, message):
    notify_functions = {
        "email": send_email,
        "slack": send_slack_message
    }

    for notify_method in NOTIFY_METHODS:
        notify_func = notify_functions[notify_method]
        if notify_func:
            notify_func(title, message)


def send_email(title, message):
    if not MAILERSEND_API_KEY or not EMAIL_RECIPIENTS or not FROM_EMAIL:
        print("Unable to send email, at least one of MAILERSEND_API_KEY, EMAIL_RECIPIENTS, FROM_EMAIL missing")
        return

    ms = MailerSendClient(api_key=MAILERSEND_API_KEY)
    builder = (EmailBuilder()
               .from_email(FROM_EMAIL, "Healthy Lunch")
               .to_many(EMAIL_RECIPIENTS)
               .subject(title)
               .text(message))

    email = builder.build()
    ms.emails.send(email)
    print("Email sent!")


def send_slack_message(title, message):
    if not SLACK_WEBHOOK_URL:
        print("Unable to trigger slack webhook, SLACK_WEBHOOK_URL missing")
        return

    r = requests.post(
        SLACK_WEBHOOK_URL,
        json={
            "title": title,
            "text": message
        },
        timeout=10,
    )
    r.raise_for_status()
    print("Slack webhook triggered!")


def get_healthy_meals(lunch_items):
    meals = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(
                fetch_item_nutrition,
                session,
                item["id"],
                item["nonce"],
            ): item
            for item in lunch_items
    }

    for future in as_completed(futures):
        item = futures[future]
        try:
            raw = future.result()
            normalized = normalize_item(raw, item["name"], item["id"])

            if allowed(normalized):
                meals.append(normalized)

        except Exception:
            continue

    return meals

def main():
    print("Starting job...")

    html = fetch_menu_page()
    lunch_items = extract_lunch_items(html)
    meals = sorted(get_healthy_meals(lunch_items), key=score, reverse=True)[:TOP_N]

    print(f"Found {len(meals)} healthy meals")

    notify(build_title(), build_message(meals))

    print("Job complete!")


if __name__ == "__main__":
    main()
