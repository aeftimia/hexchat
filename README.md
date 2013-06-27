hexchat
=======

xmpp tunnel protocol

Summary of concept:

When using hexchat, the data is sent to a local TCP socket running hexchat (call it hexchat1). Hexchat1 then reads the data (thereby stripping it of its TCP header) and passes it over a chat server to another hexchat program (call it hexchat2) that sends the data to the appropriate ip:port (giving it a new TCP header in the process).
The client thinks it is sending the data to hexchat1, and the server thinks it is receiving data from hexchat2, but the data itself is never changed. It might be broken into smaller chunks or combined into bigger chunks, and it might be delivered at unpredictable rates, but it is never altered.

------------------------------------------------------------------------------------------

Summary of implementation:
When the program is started, it is told what JID(s) to use to connect to a chat server, and possibly the following optional parameters:
1. An ip:port to listen on.

2. A JID.  Connection requests will be sent to this JID when a connection is is made to the ip:port specified in (1).

3. Another ip:port. This is the ip:port the bot connected from the JID in (2) should connect to. Note that any bot can connect to any ip:port that client asks for. This could be a 127.0.0.1 address, or a remote address (but will probably be the former). I plan to add a feature in which you can provide a list of acceptable ip:ports to connect to and refuse to connect to anything else (for security). However, that is not a priority at the moment.

When a connection is made, the server and client exchange all JIDs they are using to connect to the chat server so they can each rotate through JIDs to send messages from and JIDs to send messages to. With the exception of initial connections, all messages are sent as IQ stanzas.

When data is sent, it is base 64 encoded and sent with an ID number that is incremented each time a message is sent.

When data is received, the id is checked against the id of the last message that resulted in writing data to a socket. The difference between these two ids is used as an index in a buffer. Then, messages are read from the buffer and sent to the appropriate socket.

When a socket closes, a disconnect message is sent. Like when sending messages, an id stanza is included.

When a disconnect message is received, the bot checks if there are any entries in the appropriate socket's message buffer. If not, it closes the socket and deletes it from the routing table. If there are messages in the buffer, the disconnect message is added to the buffer at the appropriate index as a string "disconnect" as though it were normal data. When a "disconnect" string is found in the buffer, the socket is closed and deleted from the bot's routing table.

------------------------------------------------------------------------------------------

Running the program:
The server is launched with:
python hexchat.py --logfile <log file> --login <serveruser1@chatserver> 'password1' <serveruser2@chatserver> 'password2' ...

and the client is launched with:

python hexchat.py --logfile <log file> --login <clientuser1@chatserver 'password1' <clientuser2@chatserver> 'password2' ... --client <local ip1> <local port1> <serveruser@chatserver1> <remote ip1> <remote port1> ...

, where serveruser@chatserver is one of the logins used when launching the server (this one cannot be a gmail account).

You can also specify a whitelist of ip:ports you will allow your bot to connect to with the --whitelist option. This is used as follows:
python hexchat.py ... --whitelist ip1 port1 ip2 port2 ...

Use the --debug flag to run the program in debug mode:

python hexchat.py --debug --logfile ...

use the --num_logins flag to specify how many times the program should login to each account (defaults to 1).

------------------------------------------------------------------------------------------

Example:
client:
python hexchat.py --logfile /tmp/hexchat --login alice@gmail.com 'alices password' bob@gmail.com 'bobs password' charlie@jabber.org 'charlies password' --client 127.0.0.1 5555 robert@jabber.org <some ip address> <some port number>

server:
python hexchat.py --logfile /tmp/hexchat --login robert@jabber.org 'roberts password' eve@gmail.com 'eves password' alicia@jabber.org 'alicias password'

Now when when the client connects to 127.0.0.1:5555, the data will be forwarded to the server, which will in turn forward the connection and data to <some ip address>:<some port number>.
