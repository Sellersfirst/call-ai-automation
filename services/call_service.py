from config.config import ELEVEN_LABS_KEY, ELEVENLABS_URL
import requests, logging, httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(funcName)s | %(message)s"
)

logger = logging.getLogger(__name__)

async def make_call(phone_id, to_number, address, agent_id):
    logger.info(f"Making call | to: {to_number}, from_id: {phone_id}, address: {address}")

    payload = {
        "agent_id": agent_id,
        "agent_phone_number_id": phone_id,
        "to_number": to_number,
        "conversation_initiation_client_data": {
            "dynamic_variables": {
                "address": address
            }
        }
    }

    headers = {
        "xi-api-key": ELEVEN_LABS_KEY,
        "Content-Type": "application/json"
    }
    async with httpx.AsyncClient() as client:
        res = await client.post(ELEVENLABS_URL, json=payload, headers=headers)
    # res = requests.post(ELEVENLABS_URL, json=payload, headers=headers)
    if res.status_code != 200:
        logger.error(f"Call failed | status={res.status_code} | response={res.text}")
        return None
    logger.info(f"ElevenLabs status code: {res.status_code}")
    try:
        response_json = res.json()
        return response_json
    except Exception:
        logger.error("Failed to parse ElevenLabs response")
        return None