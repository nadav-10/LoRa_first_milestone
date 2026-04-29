# LoRa Text \- The Router

# Overview

The LoRa text [protocol](https://docs.google.com/document/d/1R6RPOSArlqh7fkEEshfUnLZh-1TBsDNvasrkrAXGayg/edit?tab=t.0) describes some of the roles of the router as the "routing" messages are part of the protocol.   
Router is written in Python on the Raspberry Pi \- it handles the following:

* communication via the LoRa radio modem device connected to the Raspberry Pi  
* Repeating messages received from the the Radio based on routing algorithm  
* Managing routing tables with remote users/router  
* Managing communication with local users via the REST API  
* Sending all the "key-ppublish" and "user-details" messages for the users

The REST API is the interface between the Router and the JAva-Front-end server that serves browser users and mobile-application users

# The Java web Front End

The main client of the router REST API is the Java FE. The Specific implementation for the Java FE should not affect the router. However we list here the responsibilities of the Java FE So you can understand what the Router REST API is used for  
Java FE is responsible:

* Browser clients \- rendering UI in html, keeping user login, session, keeping user information \- RSA key, nick, name etc. Managing contact list if you want.  
* Managing incoming and out coming messages for each user, messaging history.  
* Sending messages to publish user key and user info periodically when the user is connected and alive.  
* Generating RSA1024 keys for new users, getting their user information.

# Development process

The router is run on two platforms:

* Laptop \- during development and testing  
* Raspberry Pi \- in later development and in the final application

The communication output of the router \- by which it connects to the entire network:

* Simulating server \- a server written by Franji with a simulation library with which we can develop and test the network even without LoRa radio devices  
* LoRa modem connected to USB \- can be used on laptop for testing and on the raspbery Pi for final application

The clients/user connect to the router in the following ways

* Mobile application talk to the Java FE which connects to the router via the REST API  
* Browser users (on laptop/phone) connect to the Java FE which connects to the router via the REST API  
* Python test client \- connects directly to the RST API to allow developing and testing without the JavaFE.

# REST API

There are several REST API requests. A request that only returns a result is a HTTP GET. A request that does a change is a POST request. For POST the content of the request is in the body of the request in a JSON format.

## Request /stats

a GET request returning a JSON with counters used to debug the state of the router. For example \- number of packets sent, received etc.  
The response is a JSON object with "status" key with values "OK" or "error".

## Request /users

a GET request. Returns a JSON array of JSON objects. Each Object has the following keys:  
key\_id, key\_b64, nick, name, phone, delay\_secs, is\_local  
The types of these fields (JSON types)  
int, string, string, string, string, int, boolean  
The response is a JSON object with "status" key with values "OK" or "error".

## Request /alive

a POST request with a single JSON object representing the status of a single user.  
The request should have the following fields \- only key\_b64 is mandatory:  
 key\_b64, name , nick, phone  
The response is a JSON object with "status" key with values "OK" or "error".  
When status is "OK" you also get field "messages" which is a list of the last messages received for this user. The same last messages repeat in every call to /alive \- it is the responsibility of the FE/mobile-app to show only the new messages.  
For example:  
`{"status": "OK", "messages": []}`  
or  
`{"status": "error", "error": "Invalid key_b64"}`

each message in the "messages" array is an object with the following fields:

| field | type |  |
| :---- | :---- | :---- |
| utime | int | 32 bit unix time UTC sent in big endian. This allows measuring how long it took the message to arrive. |
| sender | int | key id of the sending user |
| to | int | key id of the receiving user |
| crypt2\_b64 OR plain2\_b64 | string | base64 encoded bytes of the message. If the name of the field is  "plain2\_b64" \- the message is not encrypted. if the field name is "crypt2\_b64" it is the message  encrypted with receiver public key and then  reverse-encrypted with the sender private key . See content in the [protocol doc](https://docs.google.com/document/d/1R6RPOSArlqh7fkEEshfUnLZh-1TBsDNvasrkrAXGayg/edit?tab=t.0#bookmark=id.t53h83rmf4mm). |

## Request /text

a POST message to send encrypted text messages. The body of the request is a JSON object with the following fields

| field | type |  |
| :---- | :---- | :---- |
| utime | int | 32 bit unix time UTC sent in big endian. This allows measuring how long it took the message to arrive. |
| sender | int | key id of the sending user |
| to | int | key id of the receiving user |
| crypt2\_b64 | string | bytes encrypted with receiver public key and then  reverse-encrypted with sender private key See content in the [protocol doc](https://docs.google.com/document/d/1R6RPOSArlqh7fkEEshfUnLZh-1TBsDNvasrkrAXGayg/edit?tab=t.0#bookmark=id.t53h83rmf4mm).  |

The response is a JSON object with "status" key with values "OK" or "error".

### 

## Request /plain

a POST message to send plain (NOT encrypted) text messages. The body of the request is a JSON object with the following fields

| field | type |  |
| :---- | :---- | :---- |
| utime | int | 32 bit unix time UTC sent in big endian. This allows measuring how long it took the message to arrive. |
| sender | int | key id of the sending user |
| to | int | key id of the receiving user |
| plain2\_b64 | string | bytes encrypted with receiver public key and then  reverse-encrypted with sender private key See content in the [protocol doc](https://docs.google.com/document/d/1R6RPOSArlqh7fkEEshfUnLZh-1TBsDNvasrkrAXGayg/edit?tab=t.0#bookmark=kix.347kzd5yq0qj).  |

# References

Project [overview](https://docs.google.com/presentation/d/1G__Wx5-0q-9JUBvRWD-sc30z9V4-2L57mxG_1ymOR34/edit?slide=id.g340ecfa7e62_0_82#slide=id.g340ecfa7e62_0_82)  
LoRa Text [protocol](https://docs.google.com/document/d/1R6RPOSArlqh7fkEEshfUnLZh-1TBsDNvasrkrAXGayg/edit?tab=t.0) (has the encryption code)  
The [Router](https://docs.google.com/document/d/1edV4Ms6Z1XcKJKU77FARA5k-Ma26hD-IJX1rgUCRskA/edit?usp=sharing)  
LoRa USB modem SX1262 [specs](https://www.waveshare.com/usb-to-lora.htm)

