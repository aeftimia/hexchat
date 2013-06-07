hexchat
=======

xmpp tunnel protocol

This program sends and recieves network traffic over an XMPP chatline. Everything needed to establish and maintain connections is in the .config file. Pass the config file as a command line arguement when using this program. 

The clients really do all the work in determining what goes where, while the servers are in a promiscuous state. Both log into an XMPP server. The traffic, as usual, starts with the client. The client's config file specifies one or more XMPP chat accounts it should use when communicating. Under each account, there is a line of the form:
localip:port==>JID==>remoteip:port
which states that traffic going to a local ip and port should be forwarded to some user, JID, over the XMPP chat line and arrive at a remote ip and port. The server should be logged in using the same hexchat program using the JID specified in the client's config file. So a client's config file might look like this:
client@XMPPserver.com:password:
localhost:12345==>server@XMPPserver==>localhost:22

and a server's config file might look like this:
server@XMPPserver.com:password:

When using this program, simply have all computers participating in the port forwarding process running instance of this program with the proper config files. In this case, a user on the client machine would ssh to localhost:12345. The client's program would send a connection request to server@XMPPserver and specify the destination port (localhost:22) in the subject xml tag. The client also specifies the port of the connected TCP socket in the "from nickname" xml tag. The server establishes a connection to the requested port, and adds an entry to its routing table. At this point, client and server are indistinguishable. Both go through the same process of recieving inbound data, redirecting it to the port in the subject tag, and sending outbound data to the proper remote ip:port combination filling out the subject, "from nickname" tags to determine the data's destination and source respectively. 
