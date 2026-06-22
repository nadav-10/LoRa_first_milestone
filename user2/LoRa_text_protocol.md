# LoRa Text protocol

All messages are described in binary format. 

# Messages

## message: Key publish

| field | size bytes |  |
| :---- | :---- | :---- |
| AE | 1 | constant '\\xAE' |
| kind | 1 | message kind. 01 \== "key publish" |
| utime | 4 | 32 bit unix time UTC sent in big endian. This allows measuring how long it took the message to arrive. |
| sender | 4 | key\_id \- 4 first bytes of the public key. Interpreted as 32bit integer big endian |
| key2 | 124 | a public key of a user. The first 4 bytes are the "key\_id" \- so together the key is 128 bytes |

The router should repeat this message if the router just sees this user for the first time.  
If this is a key publish for an existing user \- do only periodic forwarding with some random selection.

`# struct.pack format "!BBII124s"  OR "!BBI128s"`

## message: plain (debugging only)

See also message "text" above. This is the unencrypted version of it.

| field | size bytes |  |
| :---- | :---- | :---- |
| AE | 1 | constant '\\xAE' |
| kind | 1 | message kind. 05 \== "plain" |
| utime | 4 | 32 bit unix time UTC sent in big endian. This allows measuring how long it took the message to arrive. |
| sender | 4 | key id of the sending user |
| to | 4 | key id of the receiving user |
| plain2 | 3..128 | See below bytes of the 'plain2' fields  |

`# struct.pack format "!BBIII" for the first 14 bytes - rest parsed as described in 'plain2'`

Fields inside plain2

| field | size bytes |  |
| :---- | :---- | :---- |
| seq | 1 | Message sequence number.starts with 1 and  after 255 comes back to 1\.This number counts the messages between this sender and this 'to' recipient. each sender-\>to have a separate counter. |
| ack\_seq | 1 | if \>0 \- acknowledges receiving message with seq==ack\_seq |
| text\_len | 1 | 0..125. if 0 \- seq num should be 0 and is ignored. This means you can sent a message with no text only for ack\_seq. It will still take 128 bytes because of encryption so it is very wasteful. |
| text\_utf8 | 0..125 | utf8 encoded text |

\# struct.pack format "BBB" \+ rest of bytes as the text

## message: User details (Optional)

User is not obliged to send this message. Two users can exchange text messages only based on their public key sent in message "key publish"

| field | size bytes |  |
| :---- | :---- | :---- |
| AE | 1 | constant '\\xAE' |
| kind | 1 | message kind. 02 \== "user details" |
| utime | 4 | 32 bit unix time UTC sent in big endian. This allows measuring how long it took the message to arrive. |
| sender | 4 | key id of the sending user |
| crypt | 128 | bytes reverse-encrypted with sender private key |

`# struct.pack format "!BBII128s"`

Fields inside crypt

| field | size bytes |  |
| :---- | :---- | :---- |
| text\_len | 1 | 0..121 \- if 0 text\_utf8 is ignored and any old user info is preserved. This is useful if we only want to send the utime field as "I am alive" message. In any case the full message will still be 128 bytes because of encryption. |
| text\_utf8 | 0..127 | utf8 encoded text in the following format: nick:name:phone\_numAll fields are optional \- for example if I only want to send a nick the field will be Moses:: |

The router should repeat this message if the router just seen this user for the first time.  
If this is a key publish for an existing user \- do only periodic forwarding with some random selection.  
(TODO:franji \- should we prefer short delays or long?)

## message: text (encrypted)

See also message "plain" below which is used to send un encrypted, unsigned text.

| field | size bytes |  |
| :---- | :---- | :---- |
| AE | 1 | constant '\\xAE' |
| kind | 1 | message kind. 03 \== "text" |
| utime | 4 | 32 bit unix time UTC sent in big endian. This allows measuring how long it took the message to arrive. |
| sender | 4 | key id of the sending user |
| to | 4 | key id of the receiving user |
| crypt2 | 128 | bytes encrypted with receiver public key and then  reverse-encrypted with sender private key  |

`# struct.pack format "!BBIII128s"`

Fields inside crypt2

| field | size bytes |  |
| :---- | :---- | :---- |
| seq | 1 | Message sequence number.starts with 1 and  after 255 comes back to 1\.This number counts the messages between this sender and this 'to' recipient. each sender-\>to have a separate counter. |
| ack\_seq | 1 | if \>0 \- acknowledges receiving message with seq==ack\_seq |
| text\_len | 1 | 0..125. if 0 \- seq num should be 0 and is ignored. This means you can sent a message with no text only for ack\_seq. It will still take 128 bytes because of encryption so it is very wasteful. |
| text\_utf8 | 0..125 | utf8 encoded text |

\# struct.pack format "BBB" \+ rest of bytes as the text

The router should keep for each message a hash of (sender, to, crypt2) to identify a message and count how many times it has received it. If seen for the first time the router should repeat it.  If not the router should repeat the message with some probability that decreases as the number of times seen increases.

## message: routing (Optional)

| field | size bytes |  |
| :---- | :---- | :---- |
| AE | 1 | constant '\\xAE' |
| kind | 1 | message kind. 04 \== "router" |
| utime | 4 | 32 bit unix time UTC sent in big endian. This allows measuring how long it took the message to arrive. |
| sender | 4 | key id of the sending user. Usually this is not a normal user \- this is a router. |
| crypt | 128 | bytes reverse-encrypted with sender (router) private key |

`# struct.pack format "!BBII128s"`

Fields inside crypt

| field | size bytes |  |
| :---- | :---- | :---- |
| rec\_num | 1 | Number of records in the 'nodes' list. 0..20 |
| nodes | 6 bytes \* 0..20 | each record is 6 bytes (see below) describing the network status of a node |

\# struct.pack format "B" \-  only for the fields without 'nodes'

Node record inside nodes list:

| field | size bytes |  |
| :---- | :---- | :---- |
| seconds\_delay | 1 | delay in SECONDS between the time of the router and the time on the packet received from 'sender'. if more than one packet received in the last hour \- average of the last 3 delays. if \>255 \- leave as 255 |
| minutes\_ago | 1 | number of MINUTES passed from last message seen from 'sender'. If received in the last minute \=0. If \> 255 \- leave 255 |
| sender | 4 | key\_id of the node router seen |

`# struct.pack format "!BBI" -  for each node`

Routers can send routing messages periodically.  
Routers should prefer sending records of users they've seen lately and in a small delay.  
For users they have not seen and received from another router \- they should add they user delay to the delay they have with the sending router.

If there are too many nodes in the list that have small delay \- router should choose 20 nodes in some random manner.

## message: routing-plain (Debug only)

See message "routing" above \- this is the same message but not encrypted \- to be used in debugging/development stage.

| field | size bytes |  |
| :---- | :---- | :---- |
| AE | 1 | constant '\\xAE' |
| kind | 1 | message kind. 06 \== "router-plain" |
| utime | 4 | 32 bit unix time UTC sent in big endian. This allows measuring how long it took the message to arrive. |
| sender | 4 | key id of the sending user. Usually this is not a normal user \- this is a router. |
| nodes | 128 or less | See "crypt" field in "routing" message above |

`# struct.pack format "!BBII" + bytes(nodes)`

# References

Project [overview](https://docs.google.com/presentation/d/1G__Wx5-0q-9JUBvRWD-sc30z9V4-2L57mxG_1ymOR34/edit?slide=id.g340ecfa7e62_0_82#slide=id.g340ecfa7e62_0_82)  
LoRa Text [protocol]()  
The [Router](https://docs.google.com/document/d/1edV4Ms6Z1XcKJKU77FARA5k-Ma26hD-IJX1rgUCRskA/edit?usp=sharing)

# Appendix

## Appendix \- encrypt and sign code \- python

Note that router does not encrypt/decrypt user messages \- router only signs router messages and checks signatures for user-details messages and "routing" messages.  
Below is the [pubkey.py](http://pubkey.py) module:  
from Crypto.PublicKey import RSA  
from Crypto.Math.Numbers import Integer

\# You need \`pip install cryptography\`

RSA\_1024\_DER\_HEADER \= b'0\\x81\\x9f0\\r\\x06\\t\*\\x86H\\x86\\xf7\\r\\x01\\x01\\x01\\x05\\x00\\x03\\x81\\x8d\\x000\\x81\\x89\\x02\\x81\\x81\\x00'

def big\_int\_to\_bytes(big\_int):  
   return big\_int.to\_bytes((big\_int.bit\_length() \+ 7) // 8, byteorder\='big')

class RSA1024:  
   def \_\_init\_\_(self):  
       self.key \= None  
       self.public\_key \= None

   @staticmethod  
   def generate() \-\> 'RSA1024':  
       dst \= RSA1024()  
       dst.key \= RSA.generate(1024)  
       dst.public\_key \= dst.key.publickey()  
       return dst

   def key\_id(self) \-\> bytes:  
       return self.public\_bytes()\[:4\]

   def public\_bytes(self) \-\> bytes:  
       public\_key \= self.public\_key  
       der\_key \= public\_key.exportKey(format\='DER')  
       if not der\_key.startswith(RSA\_1024\_DER\_HEADER):  
           raise ValueError("Invalid DER key format")  
       return der\_key\[len(RSA\_1024\_DER\_HEADER):\]

   @staticmethod  
   def from\_public\_bytes(public\_bytes: bytes) \-\> 'RSA1024':  
       dst \= RSA1024()  
       dst.key \= None  
       dst.public\_key \= RSA.importKey(RSA\_1024\_DER\_HEADER \+ public\_bytes)  
       return dst

   def private\_key\_text(self) \-\> str:  
       return self.key.exportKey(format\='PEM').decode('utf-8')

   @staticmethod  
   def from\_private\_text(pem\_text: str) \-\> 'RSA1024':  
       dst \= RSA1024()  
       dst.key \= RSA.import\_key(pem\_text)  
       return dst

   def decrypt(self, ciphertext: bytes) \-\> bytes:  
       *"""use private key to decrypt."""*  
       message\_int \= int.from\_bytes(ciphertext, byteorder\='big')  
       \# Encrypt with private key (manually)  
       encrypted\_int \= self.key.\_decrypt(message\_int)  
       return big\_int\_to\_bytes(encrypted\_int)

   def encrypt(self, plaintext: bytes) \-\> bytes:  
       *"""Encript using private key"""*  
       encrypted\_int \= int.from\_bytes(plaintext, byteorder\='big')  
       decrypted\_int \= self.public\_key.\_encrypt(encrypted\_int)  
       return big\_int\_to\_bytes(decrypted\_int)

def test\_pem\_text\_export\_import():  
   bob \= RSA1024.generate()  
   bob\_pem\_text \= bob.private\_key\_text()  
   assert RSA1024.from\_private\_text(bob\_pem\_text).private\_key\_text() \== bob\_pem\_text

def test\_sign\_and\_encrypt():  
   \# create private-public pair for both alice and bob.  
   alice \= RSA1024.generate()  
   bob \= RSA1024.generate()  
   \# alice wants to send a message to Bob  
   alice\_message \= ("This is a long message..." \* 5).encode('utf-8')  
   assert len(alice\_message) \> 100 and len(alice\_message) \< 128  
   \# Bob publishes his public key as a bytes  
   bob\_public\_bytes \= bob.public\_bytes()  
   assert len(bob\_public\_bytes) \== 128  
   \# Alice can load Bob's public key from bytes into bob\_pub  
   bob\_pub \= RSA1024.from\_public\_bytes(bob\_public\_bytes)  
   assert bob\_pub.public\_bytes() \== bob\_public\_bytes  
   \# Alice does 2 things:  
   \# 1\. "decrypts" her message with the private key \- this is used to sign the message  
   \# 2\. encrypts the message with Bob's public key \- this is the encryption  
   alice2bob \= bob\_pub.encrypt(alice.decrypt(alice\_message))  
   assert len(alice2bob) \== 128  
   \# Alice publishes her public key as bytes  
   alice\_public\_bytes \= alice.public\_bytes()  
   \# Bob can load Alice's public key.  
   alice\_pub \= RSA1024.from\_public\_bytes(alice\_public\_bytes)  
   \# Bob can decrypt the message using his private key and then uses Alice's public key to make sure the message  
   \# comes from her.  
   bob\_out \= alice\_pub.encrypt(bob.decrypt(alice2bob))  
   assert bob\_out \== alice\_message

## Appendix \- encrypt and sign code \- Java

End to end encryption can be done in the Apple/Android app or in the Web-server front-end (which is written in Java). For browser user \- the private keys for the users are managed by the Java front end server. So the encrypt decrypt code in Java below is useful:

package il.ac.tau.cs.experiment;  
import java.security.\*;  
import java.security.interfaces.\*;  
import java.security.spec.\*;  
import java.util.Arrays;  
import javax.crypto.Cipher;  
import java.util.Base64;

public class RSA1024 {  
   private PrivateKey privateKey;  
   private PublicKey publicKey;

   public static RSA1024 generate() throws GeneralSecurityException {  
       KeyPairGenerator kpg \= KeyPairGenerator.*getInstance*("RSA");  
       kpg.initialize(1024);  
       KeyPair kp \= kpg.generateKeyPair();  
       RSA1024 rsa \= new RSA1024();  
       rsa.privateKey \= kp.getPrivate();  
       rsa.publicKey \= kp.getPublic();  
       return rsa;  
   }

   public byte\[\] keyId() throws GeneralSecurityException {  
       return Arrays.*copyOf*(publicBytes(), 4);  
   }

   public byte\[\] publicBytes() throws GeneralSecurityException {  
       return publicKey.getEncoded(); // DER format (X.509)  
   }

   public static RSA1024 fromPublicBytes(byte\[\] bytes) throws GeneralSecurityException {  
       X509EncodedKeySpec spec \= new X509EncodedKeySpec(bytes);  
       KeyFactory factory \= KeyFactory.*getInstance*("RSA");  
       RSA1024 rsa \= new RSA1024();  
       rsa.publicKey \= factory.generatePublic(spec);  
       return rsa;  
   }

   public String privateKeyText() {  
       return Base64.*getEncoder*().encodeToString(privateKey.getEncoded());  
   }

   public static RSA1024 fromPrivateText(String pemBase64) throws GeneralSecurityException {  
       byte\[\] encoded \= Base64.*getDecoder*().decode(pemBase64);  
       PKCS8EncodedKeySpec spec \= new PKCS8EncodedKeySpec(encoded);  
       KeyFactory factory \= KeyFactory.*getInstance*("RSA");  
       RSA1024 rsa \= new RSA1024();  
       rsa.privateKey \= factory.generatePrivate(spec);  
       rsa.publicKey \= factory.generatePublic(new RSAPublicKeySpec(  
               ((RSAPrivateCrtKey) rsa.privateKey).getModulus(),  
               ((RSAPrivateCrtKey) rsa.privateKey).getPublicExponent()  
       ));  
       return rsa;  
   }

   public byte\[\] decrypt(byte\[\] ciphertext) throws GeneralSecurityException {  
       Cipher cipher \= Cipher.*getInstance*("RSA/ECB/NoPadding");  
       cipher.init(Cipher.*DECRYPT\_MODE*, privateKey);  
       return stripLeadingZeros(cipher.doFinal(ciphertext));  
   }

   public byte\[\] encrypt(byte\[\] plaintext) throws GeneralSecurityException {  
       Cipher cipher \= Cipher.*getInstance*("RSA/ECB/NoPadding");  
       cipher.init(Cipher.*ENCRYPT\_MODE*, publicKey);  
       return stripLeadingZeros(cipher.doFinal(plaintext));  
   }

   private byte\[\] stripLeadingZeros(byte\[\] input) {  
       int start \= 0;  
       while (start \< input.length && input\[start\] \== 0) {  
           start++;  
       }  
       return Arrays.*copyOfRange*(input, start, input.length);  
   }

   static void testPemExportImport() throws GeneralSecurityException {  
       RSA1024 bob \= RSA1024.*generate*();  
       String pem \= bob.privateKeyText();  
       RSA1024 bob2 \= RSA1024.*fromPrivateText*(pem);  
       assert(pem.equals(bob2.privateKeyText()));  
   }

   static void testSignAndEncrypt() throws GeneralSecurityException {  
       RSA1024 alice \= RSA1024.*generate*();  
       RSA1024 bob \= RSA1024.*generate*();

       String msg \= "This is a long message...".repeat(5);  
       byte\[\] aliceMessage \= msg.getBytes();  
       assert(aliceMessage.length \> 100 && aliceMessage.length \< 128);

       byte\[\] bobPubBytes \= bob.publicBytes();  
       assert(162 \== bobPubBytes.length); // actual length of X.509 encoded 1024-bit key

       RSA1024 bobPub \= RSA1024.*fromPublicBytes*(bobPubBytes);  
       assert(bobPub.publicBytes().equals(bobPubBytes));

       byte\[\] signed \= alice.decrypt(aliceMessage); // simulates signing  
       assert(signed.length \== aliceMessage.length);  
       byte\[\] encrypted \= bobPub.encrypt(signed);  
       assert(128 \== encrypted.length);

       RSA1024 alicePub \= RSA1024.*fromPublicBytes*(alice.publicBytes());  
       byte\[\] decrypted \= bob.decrypt(encrypted);  
       byte\[\] verified \= alicePub.encrypt(decrypted);

       assert(Arrays.*equals*(verified, aliceMessage));  
   }  
   public static void main(String\[\] args) throws GeneralSecurityException {  
       *testPemExportImport*();  
       *testSignAndEncrypt*();  
   }  
}

