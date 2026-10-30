import requests

def send_telegram_message(token, chat_id, message):
    if not token or not chat_id:
        print("Telegram Token of Chat ID ontbreekt.")
        return
    
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown"
    }
    
    try:
        response = requests.post(url, json=payload)
        if not response.ok:
            print(f"Fout bij verzenden Telegram bericht: {response.text}")
    except Exception as e:
        print(f"Telegram netwerkfout: {e}")
