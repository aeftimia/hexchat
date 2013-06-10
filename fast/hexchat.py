#!/usr/bin/env python
import asyncore
import logging
import socket
import sleekxmpp
import sys
import base64
import time

# Python versions before 3.0 do not use UTF-8 encoding
# by default. To ensure that Unicode is handled properly
# throughout SleekXMPP, we will set the default encoding
# ourselves to UTF-8.
if sys.version_info < (3, 0):
    reload(sys)
    sys.setdefaultencoding('utf8')
else:
    raw_input = input

"""this class exchanges data between tcp sockets and xmpp servers."""
class bot(sleekxmpp.ClientXMPP):
    def __init__(self, jid, password):
        """
        Initialize a hexchat XMPP bot. Also connect to the XMPP server.

        'jid' is the login username@chatserver
        'password' is the password to login with
        """

        # <local_address> => <listening_socket> dictionary,
        # where 'local_address' is an IP:PORT string with the locallistening address,
        # and 'listening_socket' is the socket that listens and accepts connections on that address.
        self.server_sockets={}

        # <connection_id> => <xmpp_socket> dictionary,
        # where 'connection_id' is a tuple of the form:
        # (bound ip:port on client, xmpp username of server, ip:port server should forward data to)
        # and 'xmpp_socket' is a sleekxmpp socket that speaks to the XMPP bot on the other side.
        self.client_sockets={}

        #map is a "socket map" used by asyncore
        #asyncore uses this pretty transparently, so there is no need to, worry about it too much.
        self.map = {}

        #initialize the sleekxmpp client.
        sleekxmpp.ClientXMPP.__init__(self, jid, password)

        #google is a little funny.
        #your jid ends with @gmail.com
        #but you have to connect to talk.google.com, not gmail.com
        #sleekxmpp needs to be told of this explicitely.
        if jid.find("@gmail.com")!=-1:
            self.connect_address = ("talk.google.com", 5222)
        else:
            self.connect_address = None

        #event handlers are sleekxmpp's way of dealing with important xml tags it recieves
        #the only unusual event handler here is the one for "message".
        #this is set to get_message and is used to filter data recieved over the chat server
        self.add_event_handler("session_start", self.session_start)
        self.add_event_handler("disconnected", self.disconnected)
        self.add_event_handler("message", self.get_message)

        #The scheduler is xmpp's multithreaded todo list
        #This line adds asyncore's loop to the todo list
        #It tells the scheduler to evaluate asyncore.loop(0.0, True, self.map, 1)
        self.scheduler.add("asyncore loop", 0.001, asyncore.loop, (0.0, True, self.map, 1), repeat=True)

        # Connect to XMPP server
        if self.connect(self.connect_address):
            self.process()
        else:
            raise(Exception(jid+" could not connect"))

    def session_start(self, event):
        """Called when the bot connects and establishes a session with the XMPP server."""

        # XMPP spec says that we should broadcast our presence when we connect.
        self.send_presence()

    def disconnected(self, event):
        """Called when the bot disconnects from the XMPP server.
        Try to reconnect.
        """

        logging.warn("XMPP chat server disconnected")
        logging.debug("Trying to reconnect")
        if self.connect(self.connect_address):
            logging.debug("connection reestabilshed")
        else:
            raise(Exception(jid+" could not connect"))

    def get_message(self, msg):
        """Handles incoming xmpp messages and directs them to the
        proper socket
        """

        #logging.debug(msg['subject']+"<=="+msg['nick']['nick']+":"+msg['body'])

        #construct a potential client sockets key from xml data
        key = (msg['subject'],msg['from'].bare,msg['nick']['nick'])
        if key in self.client_sockets:
            #_ = blank message. xmpp does not ordinarily send blank messages,
            #so _ is used to signify it.
            if msg['body']=="_":
                self.client_sockets[key].send(b'')
            elif msg['body']=="disconnect me!":
                self.client_sockets[key].close()
                del(self.client_sockets[key])
            else:
                self.client_sockets[key].send(base64.b64decode(msg['body'].encode("UTF-8")))

        elif msg['body']=='connect me!':
            sock=socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setblocking(0)
            #portaddr_split is just where the IP ends and the port begins
            #i.e. the location of the first ":"
            portaddr_split=msg['subject'].rfind(':')
            if portaddr_split!=-1:
                try:
                    #connect the socket to the ip:port specified in the subject tag
                    if sock.connect_ex((msg['subject'][:portaddr_split], int(msg['subject'][portaddr_split+1:]))):
                        logging.debug("connecting to "+msg['subject'])
                        #add the socket to bot's client_sockets
                        self.add_client_socket(msg['subject'], msg['from'].bare, msg['nick']['nick'], sock)
                    else:
                        logging.warn("could not connect to "+msg['subject'])
                        #if it could not connect, tell the bot on the the other side to disconnect
                        self.sendMessageWrapper(msg['from'].bare, msg['subject'], msg['nick']['nick'], "disconnect me!", 'chat')
                except (socket.error, OverflowError, ValueError):
                        logging.warn("could not connect to "+msg['subject'])
                        #if it could not connect, tell the bot on the the other side to disconnect
                        self.sendMessageWrapper(msg['from'].bare, msg['subject'], msg['nick']['nick'], "disconnect me!", 'chat')
                        
        else: # The key was not found in the client_sockets routing table and it was not a connect request.
            #Dropped packets seems to be the biggest bottleneck in this connection
            #Since the sockets are using tcp, they think the other party has sent and recieved data
            #by the time the message is piped to the xmpp server.
            #The problem is if one party disconnects while the other sends a message, the packet has to be dropped
            #This leads to some degree of unreliability
            #It seems that without raw sockets, this bottleneck is an inherent flaw in the xmpp tunnel design.
            logging.debug('packet dropped')

    def handle_read(self, local_address, peer, remote_address):
        """Called when a TCP socket has stuff to be read from it."""

        key = (local_address,peer,remote_address)
        data=base64.b64encode(self.client_sockets[key].recv(8192)).decode("UTF-8")

        #remember, you generally cannot send blank messages over xmpp
        #so blank messages are represented by a "_"
        if not data:
            data = "_"

        self.sendMessageWrapper(peer, local_address, remote_address, data, 'chat')

    def handle_accept(self, local_address, peer, remote_address):
        """Called when we have a new incoming connection to one of our listening sockets."""

        connection, local_address = self.server_sockets[local_address].accept()
        local_address=local_address[0]+":"+str(local_address[1])
        #add the new connected socket to client_sockets
        self.add_client_socket(local_address, peer, remote_address, connection)
        #send a connection request to the bot waiting on the other side of the xmpp server
        self.sendMessageWrapper(peer, local_address, remote_address, 'connect me!', 'chat')

    #this is the function that gets called when a tcp client socket gets disconnected
    #or is otherwise about to close
    def handle_close(self, key):
        if key in self.client_sockets:
            self.client_sockets[key].close()
            del(self.client_sockets[key])
            local_address, peer, remote_address = key
            #send a disconnection request to the bot waiting on the other side of the xmpp server
            self.sendMessageWrapper(peer, local_address, remote_address, 'disconnect me!', 'chat')

    def sendMessageWrapper(self, mto0, mnick0, msubject0, mbody0, mtype0):
        """Wrapper over sleekxmpp's sendMessage function.

        * mto - The recipient of the message.
        * mbody - The main contents of the message.
        * msubject - Optional subject for the message.
        * mtype - The message's type, such as 'chat' or 'groupchat'.
        * mnick - Optional nickname of the sender.
        """

        #logging.debug(mnick0+"==>"+msubject0+":"+mbody0)
        self.sendMessage(mto=mto0, mnick=mnick0, msubject=msubject0, mbody=mbody0, mtype=mtype0)

    def add_client_socket(self, local_address, peer, remote_address, sock):
        """Add socket to the bot's routing table."""

        assert(sock)

        # Calculate the client_sockets identifier for this socket, and put in the dict.
        key=(local_address,peer,remote_address)
        self.client_sockets[key] = asyncore.dispatcher(sock, map=self.map)

        #just some asyncore initialization stuff
        self.client_sockets[key].writable=lambda: False
        self.client_sockets[key].handle_read=lambda: self.handle_read(local_address, peer, remote_address)
        self.client_sockets[key].handle_close=lambda: self.handle_close(key)

    def add_server_socket(self, local_address, peer, remote_address):
        """Create a listener and put it in the server_sockets dictionary."""

        portaddr_split=local_address.rfind(':')
        if portaddr_split == -1:
            raise(Exception("No port specified"+local_address))

        self.server_sockets[local_address] = asyncore.dispatcher(map=self.map)

        #just some asyncore initialization stuff
        self.server_sockets[local_address].create_socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_sockets[local_address].writable=lambda: False
        self.server_sockets[local_address].set_reuse_addr()
        self.server_sockets[local_address].bind((local_address[:portaddr_split], int(local_address[portaddr_split+1:])))
        self.server_sockets[local_address].handle_accept = lambda: self.handle_accept(local_address, peer, remote_address)
        self.server_sockets[local_address].listen(1023)

if __name__ == '__main__':
    logging.basicConfig(filename=sys.argv[2],level=logging.WARN)
    if sys.argv[1]=="-c":
        if not len(sys.argv) in (5,10):
            raise(Exception("Wrong number of command line arguements"))
        else:
            username=sys.argv[3]
            password=sys.argv[4]
            bot0=bot(username, password)
            if len(sys.argv)==10:
                bot0.add_server_socket(sys.argv[5]+":"+sys.argv[6], sys.argv[7], sys.argv[8]+":"+sys.argv[9])
    else:
        if len(sys.argv)!=3:
            raise(Exception("Wrong number of command line arguements"))
        bots={}
        fd=open(sys.argv[1])
        lines=fd.read().splitlines()
        fd.close()
        for line in lines:
            #if the line is of the form username@chatserver:password:
            if line[-1]==":":
                userpass_split=line.find(':')
                try:
                    username=line[:userpass_split]
                    bots[username]=bot(username, line[userpass_split+1:-1])
                    continue
                except IndexError:
                    raise(Exception("No password supplied."))
            [local_address, peer, remote_address]=line.split('==>')
            #add a server socket listening for incomming connections
            try:
                bots[username].add_server_socket(local_address, peer, remote_address)
            except (OverflowError, socket.error, ValueError) as msg:
                raise(msg)
                
    #program needs to be kept running on linux
    while True:
        time.sleep(1)
