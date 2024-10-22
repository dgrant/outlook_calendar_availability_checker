import argparse
import requests
from requests.adapters import HTTPAdapter, Retry
from datetime import datetime, timedelta, timezone
import logging
import time
from twilio.rest import Client
import yaml
import os
import pytz

# Constants
POLLING_INTERVAL_DEFAULT = 60

# Set up logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Twilio HTTP client logger
twilio_logger = logging.getLogger("twilio.http_client")
twilio_logger.setLevel(logging.WARNING)  # Quiet Twilio logs


def find_config_file(filename='config.yaml'):
    """
    Search for the config file up the directory tree.
    """
    current_dir = os.path.abspath(os.getcwd())

    while True:
        config_path = os.path.join(current_dir, filename)
        if os.path.exists(config_path):
            return config_path

        # Move one level up
        parent_dir = os.path.dirname(current_dir)

        # If reached the root directory, stop searching
        if current_dir == parent_dir:
            break

        current_dir = parent_dir

    return None


def load_config(config_file='config.yaml'):
    """
    Load configuration from YAML file.
    """
    config_path = find_config_file(config_file)

    if not config_path:
        raise FileNotFoundError(f"'{config_file}' not found in any parent directories.")

    # Change working directory to the directory containing the config file
    os.chdir(os.path.dirname(config_path))
    logger.info(f"Using config file at: {config_path}")

    with open(config_path, 'r') as file:
        return yaml.safe_load(file)


# Load configuration from YAML file
config = load_config()

# Timezone from config
TIMEZONE = pytz.timezone(config.get('timezone', 'America/Los_Angeles'))

# URLs for the GET and POST requests
OUTLOOK_EMAIL = config['outlook']['email']
GET_TOKEN = config['outlook']['get_token']
GET_URL = f"https://outlook.office365.com/book/{OUTLOOK_EMAIL}/s/{GET_TOKEN}"
POST_URL = f"https://outlook.office365.com/BookingsService/api/V1/bookingBusinessesc2/{OUTLOOK_EMAIL}/GetStaffAvailability?app=BookingsC2&n=7"

# Outlook settings
SERVICE_ID = config['outlook']['service_id']
STAFF_IDS = config['outlook']['staff_ids']

# Twilio credentials
ACCOUNT_SID = config['twilio']['account_sid']
AUTH_TOKEN = config['twilio']['auth_token']
TWILIO_PHONE_NUMBER = config['twilio']['phone_number']
RECIPIENT_PHONE_NUMBERS = config['recipients']

# Initial setup for the requests session with retries
session = requests.Session()
retries = Retry(total=3, backoff_factor=5, status_forcelist=[500, 502, 503, 504])
session.mount('https://', HTTPAdapter(max_retries=retries))


def send_notification(message):
    """
    Send SMS notification to multiple recipients using Twilio.
    """
    if not RECIPIENT_PHONE_NUMBERS:
        logger.error("No recipients specified for notifications.")
        return

    try:
        client = Client(ACCOUNT_SID, AUTH_TOKEN)
        for recipient in RECIPIENT_PHONE_NUMBERS:
            recipient = recipient.strip()
            if recipient:
                twilio_message = client.messages.create(
                    from_=TWILIO_PHONE_NUMBER,
                    body=message,
                    to=recipient
                )
                logger.debug(f"Notification sent to {recipient}. Message SID: {twilio_message.sid}")
    except Exception as e:
        logger.error(f"Failed to send SMS via Twilio: {e}")


def check_availability(polling_interval=POLLING_INTERVAL_DEFAULT, send_notification_test=False):
    """
    Main function to check booking availability and send notifications.
    """
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.5735.90 Safari/537.36'
    }

    while True:
        try:
            response = session.get(GET_URL, headers=headers)
            if response.status_code != 200:
                logger.error(f"GET request failed: {response.status_code} - {response.text}")
                time.sleep(polling_interval)
                continue

            logger.info("GET request successful. Proceeding with POST request...")
            payload = _create_post_payload()

            post_response = session.post(POST_URL, json=payload, headers=headers)
            if post_response.status_code != 200:
                logger.error(f"POST request failed: {post_response.status_code} - {post_response.text}")
                time.sleep(polling_interval)
                continue

            logger.info("POST request successful. Parsing response...")
            try:
                data = post_response.json()
                logger.debug(f"Response data: {data}")
                _parse_data(data, send_notification_test=send_notification_test)
            except ValueError as e:
                logger.error(f"Failed to parse JSON response: {e}")

        except requests.exceptions.RequestException as e:
            logger.error(f"Network error occurred: {e}")

        logger.info(f"Waiting for {polling_interval} seconds before checking again...")
        time.sleep(polling_interval)


def _create_post_payload():
    """
    Create the payload for the POST request.
    """
    today = datetime.now(timezone.utc)
    start_date = today - timedelta(days=1)
    end_date = start_date + timedelta(days=12)
    start_date_str = start_date.strftime("%Y-%m-%dT00:00:00")
    end_date_str = end_date.strftime("%Y-%m-%dT00:00:00")

    payload = {
        "serviceId": SERVICE_ID,
        "staffIds": STAFF_IDS,
        "startDateTime": {
            "dateTime": start_date_str,
            "timeZone": "Pacific Standard Time"
        },
        "endDateTime": {
            "dateTime": end_date_str,
            "timeZone": "Pacific Standard Time"
        }
    }
    return payload


def _parse_data(data, send_notification_test=False):
    """
    Parse response data and find available slots.
    Raises an exception if the data format is not as expected.
    """
    if send_notification_test:
        available_slots = [{"startDateTime": "2024-10-22T18:00:00", "endDateTime": "2024-10-22T18:30:00"}]
    else:
        staff_response = data.get("staffAvailabilityResponse")
        if not staff_response:
            raise ValueError("Missing 'staffAvailabilityResponse' in response.")

        available_slots = []
        for staff in staff_response:
            availability_items = staff.get("availabilityItems")
            if availability_items is None:
                raise ValueError("Missing 'availabilityItems' in staff data.")

            for item in availability_items:
                status = item.get("status")
                if status in ["BOOKINGSAVAILABILITYSTATUS_BUSY", "BOOKINGSAVAILABILITYSTATUS_OUT_OF_OFFICE"]:
                    continue

                start_time = item.get("startDateTime", {}).get("dateTime")
                end_time = item.get("endDateTime", {}).get("dateTime")

                if not start_time or not end_time:
                    raise ValueError("Missing 'startDateTime' or 'endDateTime' in item.")

                available_slots.append({
                    "startDateTime": start_time,
                    "endDateTime": end_time
                })

    if available_slots:
        formatted_slots = _format_available_slots(available_slots)
        logger.info("Available slots found!")
        send_notification(f"Booking Slots Available!\n\n{formatted_slots}\n\nGo to: {GET_URL}")
    else:
        logger.info("No available slots found.")


def _format_available_slots(slots):
    """
    Format available slots to the configured timezone without time zone info.
    """
    formatted_slots = []
    for slot in slots:
        start_dt = datetime.fromisoformat(slot['startDateTime']).astimezone(TIMEZONE)
        end_dt = datetime.fromisoformat(slot['endDateTime']).astimezone(TIMEZONE)
        formatted_slots.append(f"{start_dt.strftime('%b %d %I:%M%p')} - {end_dt.strftime('%I:%M%p')}")
    return "\n".join(formatted_slots)


def main():
    parser = argparse.ArgumentParser(description="Outlook Calendar Slot Checker")
    parser.add_argument(
        "--send-notification",
        action="store_true",
        help="Send a test notification via Twilio"
    )
    parser.add_argument(
        "--polling-interval",
        type=int,
        default=POLLING_INTERVAL_DEFAULT,
        help="Polling interval in seconds (default: 60)"
    )

    args = parser.parse_args()
    check_availability(polling_interval=args.polling_interval, send_notification_test=args.send_notification)


if __name__ == "__main__":
    main()
