from typing import Dict, Any

failed_requests: Dict[int, Dict[str, Any]] = {}   
ongoing_requests: Dict[int, bool] = {}            
user_last_action_ts: Dict[int, float] = {}        
expansion_requests: Dict[int, str] = {}           

last_button_messages: Dict[int, int] = {}

def store_failed_request(chat_id: int, user_id: int, prompt: str, original_text: str, error_message_id: int):
    failed_requests[chat_id] = {
        "user_id": user_id,
        "prompt": prompt,
        "original_text": original_text,
        "attempts_manual": 0,
        "attempts_auto": 0,
        "error_message_id": error_message_id,
        "last_attempt_ts": None,
    }

def clear_failed_request(chat_id: int):
    if chat_id in failed_requests:
        del failed_requests[chat_id]
    if chat_id in ongoing_requests:
        del ongoing_requests[chat_id]