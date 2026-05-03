from flask import Flask, jsonify, request
from lora_common.loramodem import LoRaModem
import time
import base64, threading, random
import struct
import queue
import hashlib

app = Flask(__name__)

# users_registry stores the full object required by the /users endpoint
# key: key_b64 (string)
# value: dictionary with key_id, nick, name, phone, delay_secs, is_local
users_registry = {}
message_inbox = {}
next_publish = {}; next_details = {}
messages_sent : set = set([])
messages_sent_history : list = []
pending_keys = set()
keys_lock = threading.Lock()

#all stats for /stats endpoint
server_stats = {
    "local_messages_sent": 0,
    "global_messages_sent": 0,
    "valid_alive_requests": 0,
    "users_requests_count": 0
}

# Messages waiting to be sent over the LoRa radio
# הגדרת התורים (כל אחד עם מקסימום גודל למניעת Starvation של זיכרון)
queues = {
    1: queue.Queue(maxsize=50),  # Key Publish (קריטי)
    2: queue.Queue(maxsize=50),  # Text/Plain (חשוב)
    4: queue.Queue(maxsize=50), # Forwarding (פחות חשוב)
    5: queue.Queue(maxsize=30)   # User Details (הכי פחות דחוף)
}

def serialize_lora_message(msg_data):
    """
    הופך הודעת JSON מה-API לרצף בייטים (Binary) עבור ה-LoRa.
    מטפל גם ב-Plain (kind 5) וגם ב-Text מוצפן (kind 3).
    """
    try:
        if 'crypt2_b64' in msg_data:
            kind = 0x03
            payload_b64 = msg_data['crypt2_b64']
        else:
            kind = 0x05
            payload_b64 = msg_data['plain2_b64']

        payload_bytes = base64.b64decode(payload_b64)

        # AE (1 byte), Kind (1 byte), UTime (4 bytes), Sender (4 bytes), To (4 bytes)
        header = struct.pack('!BBIII', 
                             0xAE, 
                             kind, 
                             int(msg_data.get('utime', time.time())), 
                             msg_data['sender'], 
                             msg_data['to'])
        
        prio = 2

        hash_msg = hashlib.sha256((header + payload_bytes)).hexdigest()
        if hash_msg in messages_sent:
            if random.random() > 0.1:
                print("priodic forwarding")
                return False
            prio = 4
        else:
            messages_sent.add(hash_msg)
            messages_sent_history.append(hash_msg)
            if len(messages_sent) > 2000:
                while len(messages_sent_history) > 500:
                    oldest_message = messages_sent_history.pop(0)
                    messages_sent.remove(oldest_message)

        print("sent: " + header + payload_bytes)
        queues[prio].put((time.time(), header + payload_bytes), block = False)
        return True
    except Exception as e:
        print(f"Error serializing message: {e}")
        return False
    

def publish_user_details(user_data):
    """
    יוצר חבילת User Details בינארית ושולח ל-Worker.
    user_data: dict שמכיל key_id, nick, name, phone
    """
    try:
        # יצירת ה-Payload: חיבור השדות עם נקודה-פסיק
        # nick;name;phone
        details_str = f"{user_data.get('nick','')}:{user_data.get('name','')}:{user_data.get('phone','')}"
        details_bytes = details_str.encode('utf-8')

        # אריזה לפי הפורמט: AE, Kind(02), UTime, SenderID, ואז הטקסט
        # פורמט struct: !BBII (AE, Kind, Time, Sender) ואז ה-bytes של הטקסט
        header = struct.pack('!BBII', 
                             0xAE, 
                             0x02, 
                             int(time.time()), 
                             user_data['key_id'])
        
        full_packet = header + details_bytes
        
        # הכנסה לתור של ה-Worker (עדיפות 1 - ניהול רשת)
        queues[5].put((time.time(), full_packet), block = False)
        print(f"User Details for {user_data['key_id']} queued.")
        
    except Exception as e:
        print(f"Error publishing user details: {e}")

def route_message(msg_data : dict):
    """
    Routes a message based on the 'to' key_id.
    """
    target_key_id = msg_data.get('to')
    
    # Find if the user is local by checking our registry
    # We look for a user whose key_id matches the target
    target_user = users_registry.get(target_key_id, {})

    if target_user and target_user["is_local"]:
        # LOCAL ROUTE: Put in the inbox
        if target_key_id not in message_inbox:
            message_inbox[target_key_id] = []
        message_inbox[target_key_id].append(msg_data)
        server_stats["local_messages_sent"] += 1
        return "local"
    else:
        # REMOTE ROUTE: Put in LoRa Priority Queue
        if not serialize_lora_message(msg_data):
            return "error"
        return "remote"

@app.route('/text', methods=['POST'])
@app.route('/plain', methods=['POST'])
def handle_message():
    data = request.get_json()
    
    # Validation
    required_fields = ['utime', 'sender', 'to']
    if not all(keys in data for keys in required_fields):
        return jsonify({"status": "error", "error": "Missing mandatory fields"}), 400

    # Ensure it has either encrypted or plain payload
    if 'crypt2_b64' not in data and 'plain2_b64' not in data:
        return jsonify({"status": "error", "error": "No message content found"}), 400

    # Route the message
    route_message(data)
    
    return jsonify({
        "status": "OK",
    })



def get_key_id_from_b64(key_b64):
    """
    Extracts the 4-byte key_id from the Base64 public key.
    The first 4 bytes are the key_id.
    """
    try:
        public_bytes = base64.b64decode(key_b64)
        # Unpack the first 4 bytes as a 32-bit big-endian integer
        key_id = struct.unpack("!I", public_bytes[:4])[0]
        return key_id
    except Exception:
        return None

@app.route('/alive', methods=['POST'])
def alive():
    """
    POST request representing the status of a single user.
    Updates user info and returns pending messages.
    """
    data = request.get_json()

    # key_b64 is the only mandatory field
    if not data or 'key_b64' not in data:
        return jsonify({
            "status": "error",
            "error": "Invalid key_b64"
        }), 400

    key_b64 = data.get('key_b64')
    key_id = get_key_id_from_b64(key_b64)

    if key_id is None:
        return jsonify({"status": "error", "error": "Malformed key_b64"}), 400
    
    # Update registry with all fields required by the /users GET request
    users_registry[key_id] = {
        "key_id": key_id,
        "key_b64": key_b64,
        "nick": data.get('nick', ""),
        "name": data.get('name', ""),
        "phone": data.get('phone', ""),
        "delay_secs": 0,      # Default for local check-in
        "is_local": True      # This user is hitting our REST API directly
    }

    # Retrieve messages for this specific user
    # In a full implementation, these would come from the LoRa radio logic
    messages = message_inbox.get(key_id, [])

    server_stats["valid_alive_requests"] += 1
    
    if key_id not in next_publish or time.time() > next_publish[key_id]:
        print("hello world for debug")
        publish_key(key_b64)
        next_publish[key_id] = time.time() + 15 + random.randint(0, 10)

    if key_id not in next_details or time.time() > next_details[key_id]:
        print("hello world for debug")
        publish_user_details(users_registry[key_id])
        next_details[key_id] = time.time() + 120 + random.randint(0, 60)

    return jsonify({
        "status": "OK",
        "messages": messages
    })




@app.route('/users', methods=['GET'])
def get_users():
    """
    Returns a list of all known users (local and remote).
    As per documentation, this is a GET request returning a JSON array of objects.
    """

    server_stats["users_requests_count"] += 1
    
    return jsonify({
        "status": "OK",
        "users": list(users_registry.values()), 
    })


@app.route('/stats', methods=['GET'])
def get_stats():
    """
    Returns router statistics.
    """
    return jsonify({
        "status": "OK",
        "local_messages": server_stats["local_messages_sent"],
        "global_messages": server_stats["global_messages_sent"],
        "alive_requests": server_stats["valid_alive_requests"],
        "total_users": len(users_registry),
        "users_endpoint_calls": server_stats["users_requests_count"]
    })


def publish_key(key_b64):
    """
    יוצרת הודעת Key Publish לפי הפרוטוקול ודוחפת אותה לתור ה-LoRa.
    מיועדת להפצה ראשונית או תקופתית של המפתח הציבורי ברשת.
    """
    try:
        public_bytes = base64.b64decode(key_b64)
        
        if len(public_bytes) != 128:
            print("Error: Public key must be exactly 128 bytes")
            return False

        ae_const = 0xAE
        kind = 0x01  # Key Publish
        current_utime = int(time.time())

        sender_key_id = struct.unpack("!I", public_bytes[:4])[0]

        key2 = public_bytes[4:]
        packet = struct.pack("!BBII124s", 
                             ae_const, 
                             kind, 
                             current_utime, 
                             sender_key_id, 
                             key2)
        
        with keys_lock:
            if sender_key_id in pending_keys:
                print(f"Key for {sender_key_id} already in queue. Skipping.")
                return False
        
        queues[1].put((time.time(), packet, sender_key_id), block = False) #change later
        # אם לא קיים, נוסיף אותו לסט וגם לתור
        pending_keys.add(sender_key_id)
        print(f"Key Publish packet for ID {sender_key_id} added to queue.")
        return True

    except Exception as e:
        print(f"Failed to publish key: {e}")
        return False
    

def get_next_packet_wfq():
    r = random.random()
    # הסתברות לשירות תורים:
    # 50% לעדיפות 1, 30% לעדיפות 2, 15% לעדיפות 4, 5% לעדיפות 5
    if r < 0.50 and not queues[1].empty(): 
        return 1, queues[1].get()
    elif r < 0.80 and not queues[2].empty():
        return 2, queues[2].get()
    elif r < 0.95 and not queues[4].empty():
        return 4, queues[4].get()
    elif not queues[5].empty():
        return 5, queues[5].get()
    
    for p in [1, 2, 4, 5]:
        if not queues[p].empty():
            return p, queues[p].get()
        
    return None
    

def lora_worker(device_path):
    """
    Worker שפשוט מוציא בייטים מהתור ושולח אותם למודם.
    """
    try:
        with LoRaModem(device_path) as modem:
            modem.configure_packet_mode()
            print(f"LoRa Worker started on {device_path}")

            print("cat")
            while True:
                priority, item = get_next_packet_wfq()
                print("worker got a message")
                
                if isinstance(item[1], bytes):
                    raw_bytes = item[1]
                    entry_time = item[0]
                    if time.time() > entry_time + 500:
                        print("to much time since enter")
                        continue
                    modem.write_bytes(raw_bytes)

                    if priority == 1:
                        user_id = item[2] 
                        with keys_lock:
                            pending_keys.discard(user_id)

                    server_stats["global_messages_sent"] += 1
                    print(f"Worker sent {len(raw_bytes)} b  ytes. Priority: {priority}")
                
                else:
                    print("not byte exception")

    except Exception as e:
        print(f"LoRa Worker crashed: {e}")

if __name__ == '__main__':
    threading.Thread(target=lora_worker, args=("sim",), daemon=True).start()
    app.run(host='0.0.0.0', port=8200)