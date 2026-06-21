from Crypto.PublicKey import RSA  #pip install pycryptodome


# You need `pip install cryptography`

RSA_1024_DER_TAIL = b'\x02\x03\x01\x00\x01'
RSA_1024_DER_HEAD = b'0\x81\x9f0\r\x06\t*\x86H\x86\xf7\r\x01\x01\x01\x05\x00\x03\x81\x8d\x000\x81\x89\x02\x81\x81\x00'

def big_int_to_bytes(big_int):
    return big_int.to_bytes((big_int.bit_length() + 7) // 8, byteorder='big')


class RSA1024:
    def __init__(self):
        self.key = None
        self.public_key = None

    @staticmethod
    def generate() -> 'RSA1024':
        dst = RSA1024()
        dst.key = RSA.generate(1024)
        dst.public_key = dst.key.publickey()
        return dst

    def key_id(self) -> bytes:
        return self.public_bytes()[:4]

    def public_bytes(self) -> bytes:
        assert self.public_key is not None, "Public key is not set"
        public_key = self.public_key
        der_key = public_key.export_key(format='DER')
        if not der_key.startswith(RSA_1024_DER_HEAD):
            raise ValueError("Invalid DER key format")
        if not der_key.endswith(RSA_1024_DER_TAIL):
            raise ValueError("Invalid DER key format")
        pb = der_key[len(RSA_1024_DER_HEAD):-len(RSA_1024_DER_TAIL)]
        assert len(pb) == 128, "Public key length should be 128 bytes"
        return pb

    @staticmethod
    def from_public_bytes(public_bytes: bytes) -> 'RSA1024':
        dst = RSA1024()
        dst.key = None
        dst.public_key = RSA.importKey(RSA_1024_DER_HEAD + public_bytes + RSA_1024_DER_TAIL)
        return dst

    def private_key_text(self) -> str:
        return self.key.export_key(format='PEM').decode('utf-8')

    @staticmethod
    def from_private_text(pem_text: str) -> 'RSA1024':
        dst = RSA1024()
        dst.key = RSA.import_key(pem_text)
        dst.public_key = dst.key.publickey()
        return dst

    def decrypt(self, ciphertext: bytes) -> bytes:
        """use private key to decrypt."""
        message_int = int.from_bytes(ciphertext, byteorder='big')
        # Encrypt with private key (manually)
        encrypted_int = self.key._decrypt(message_int)
        return big_int_to_bytes(encrypted_int)

    def encrypt(self, plaintext: bytes) -> bytes:
        """Encript using private key"""
        encrypted_int = int.from_bytes(plaintext, byteorder='big')
        decrypted_int = self.public_key._encrypt(encrypted_int)
        return big_int_to_bytes(decrypted_int)


def common_prefix(a: bytes, b: bytes) -> bytes:
    """Find the common prefix of two byte strings."""
    min_length = min(len(a), len(b))
    for i in range(min_length):
        if a[i] != b[i]:
            return a[:i]
    return a[:min_length]


def test_rsa_1024_der_format_header():
    min_common_head = None
    min_common_tail = None
    for _ in range(10):
        rsa = RSA1024.generate()
        der_bytes = rsa.public_key.export_key(format='DER')
        if min_common_head is None:
            min_common_head = der_bytes
        else:
            min_common_head = common_prefix(min_common_head, der_bytes)
        if min_common_tail is None:
            min_common_tail = der_bytes
        else:
            min_common_tail = common_prefix(der_bytes[::-1], min_common_tail[::-1])
            min_common_tail = min_common_tail[::-1]
    assert min_common_tail == RSA_1024_DER_TAIL
    assert min_common_head == RSA_1024_DER_HEAD


def test_pem_text_export_import():
    bob = RSA1024.generate()
    bob_pem_text = bob.private_key_text()
    assert RSA1024.from_private_text(bob_pem_text).private_key_text() == bob_pem_text


def test_sign_and_encrypt():
    # create private-public pair for both alice and bob.
    alice = RSA1024.generate()
    bob = RSA1024.generate()
    # alice wants to send a message to Bob
    alice_message = ("This is a long message..." * 5).encode('utf-8')
    assert len(alice_message) > 100 and len(alice_message) < 128
    # Bob publishes his public key as a bytes
    bob_public_bytes = bob.public_bytes()
    assert len(bob_public_bytes) == 128
    # Alice can load Bob's public key from bytes into bob_pub
    bob_pub = RSA1024.from_public_bytes(bob_public_bytes)
    assert bob_pub.public_bytes() == bob_public_bytes
    # Alice does 2 things:
    # 1. encrypts the message with Bob's public key - this is the encryption
    # 2. "decrypts" her message with the private key - this is used to sign the message
    alice2bob = alice.decrypt(bob_pub.encrypt(alice_message))
    assert len(alice2bob) == 128
    # Alice publishes her public key as bytes
    alice_public_bytes = alice.public_bytes()
    # Bob can load Alice's public key.
    alice_pub = RSA1024.from_public_bytes(alice_public_bytes)
    # Bob can decrypt the message using his private key and then uses Alice's public key to make sure the message
    # comes from her.
    bob_out = bob.decrypt(alice_pub.encrypt(alice2bob))
    assert bob_out == alice_message

if __name__ == "__main__":
    test_rsa_1024_der_format_header()
    test_pem_text_export_import()
    test_sign_and_encrypt()
    
