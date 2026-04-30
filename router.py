from flask import Flask, jsonify, request
from lora_common.loramodem import LoRaModem
import time
import base64, threading, random
import struct
import queue

app = Flask(__name__)

# users_registry stores the full object required by the /users endpoint
# key: key_b64 (string)
# value: dictionary with key_id, nick, name, phone, delay_secs, is_local
users_registry = {}
message_inbox = {}
#ניסיון
#all stats for /stats endpoint
server_stats = {
    "local_messages_sent": 0,
    "global_messages_sent": 0,
    "valid_alive_requests": 0,
    "users_requests_count": 0
}

# Messages waiting to be sent over the LoRa radio
lora_out_queue = queue.PriorityQueue()

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
        lora_out_queue.put((2, msg_data))
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
    
    if random.random() < 0.1:
        print("hello world for debug")
        publish_key(key_b64)
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
        
        lora_out_queue.put((1, packet)) #change later
        print(f"Key Publish packet for ID {sender_key_id} added to queue.")
        return True

    except Exception as e:
        print(f"Failed to publish key: {e}")
        return False
    

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
                priority, raw_bytes = lora_out_queue.get()
                print("worker got a message")
                
                if isinstance(raw_bytes, bytes):
                    modem.write_bytes(raw_bytes)

                    server_stats["global_messages_sent"] += 1
                    print(f"Worker sent {len(raw_bytes)} bytes. Priority: {priority}")
                
                else:
                    print("not byte exception")

                lora_out_queue.task_done()
    except Exception as e:
        print(f"LoRa Worker crashed: {e}")

if __name__ == '__main__':
    threading.Thread(target=lora_worker, args=("sim",), daemon=True).start()
    app.run(host='0.0.0.0', port=8200)
