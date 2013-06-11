#!/usr/bin/env python
import asyncore
import logging
import socket
import sleekxmpp
import sys
import base64
import time
import threading

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

        #map is an opaque "socket map" used by asyncore
        self.map = {}

        #initialize the sleekxmpp client.
        sleekxmpp.ClientXMPP.__init__(self, jid, password)

        # gmail xmpp server is actually at talk.google.com
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

    ### XMPP handling methods:

    def session_start(self, event):
        """Called when the bot connects and establishes a session with the XMPP server."""

        # XMPP spec says that we should broadcast our presence when we connect.
        self.send_presence()

    def disconnected(self, event):
        """Called when the bot disconnects from the XMPP server.
        Try to reconnect.
        """

        logging.warning("XMPP chat server disconnected")
        logging.debug("Trying to reconnect")
        if self.connect(self.connect_address):
            logging.debug("connection reestabilshed")
        else:
            raise(Exception(jid+" could not connect"))

    def get_message(self, msg):
        """Handles incoming xmpp messages and directs them to the
        proper socket
        """
        msg_type=msg.get_type()

        # We want to handle 'chat' and 'error' messages.
        if msg_type not in ['error', 'chat']:
            return()

        nick = msg['nick']['nick']
        from_addr = msg['from'].bare
        subject = msg['subject']
        body = msg['body']

        if msg_type == 'error':
            key = (nick, from_addr, subject)
            if key in self.client_sockets: # if error from known client: resend the message
                self.sendMessageWrapper(from_addr, nick, subject, body, 'chat')
            return

        # If we are here, message type was 'chat'.
        assert(msg_type == 'chat')
        logging.debug(subject+"<=="+nick+":"+body)

        #construct a potential client sockets key from xml data
        key = (subject, from_addr, nick)
        if key in self.client_sockets: # known client
            if body=="_": # blank message
                # XMPP does not support blank messages so we interpret '_' as a blank message.
                self.client_sockets[key].buffer+=b''
            elif body=="disconnect me!": # client wants to kill the session
                self.client_sockets[key].close()
                del(self.client_sockets[key])
            else: # normal traffic
                try:
                    self.client_sockets[key].buffer+=base64.b64decode(body.encode("UTF-8"))
                except (UnicodeDecodeError, TypeError):
                    logging.warn("invalid data recieved")

        elif body=='connect me!': # unknown client that wants to start a hexchat session
            #try to connect to our destination and add the connected socket to the bot's client_sockets
            threading.Thread(target=lambda: self.initiate_connection(subject, from_addr, nick)).start()

        else: # unknown client that sent us random data.
            # The key was not found in the client_sockets routing table and it was not a connect request.
            #Dropped packets seems to be the biggest bottleneck in this connection
            #Since the sockets are using tcp, they think the other party has sent and recieved data
            #by the time the message is piped to the xmpp server.
            #The problem is if one party disconnects while the other sends a message, the packet has to be dropped
            #This leads to some degree of unreliability
            #It seems that without raw sockets, this bottleneck is an inherent flaw in the xmpp tunnel design.
            logging.debug('packet dropped')

    def sendMessageWrapper(self, mto0, mnick0, msubject0, mbody0, mtype0):
        """Wrapper over sleekxmpp's sendMessage function.

        * mto - The recipient of the message.
        * mbody - The main contents of the message.
        * msubject - Optional subject for the message.
        * mtype - The message's type, such as 'chat' or 'groupchat'.
        * mnick - Optional nickname of the sender.
        """

        logging.debug(mnick0+"==>"+msubject0+":"+mbody0)
        self.sendMessage(mto=mto0, mnick=mnick0, msubject=msubject0, mbody=mbody0, mtype=mtype0)

    ### Methods for connection/socket creation.

    def initiate_connection(self, local_address, peer, remote_address):
        """Initiate connection to 'local_address' and add the socket to the client sockets map."""

        #portaddr_split is just where the IP ends and the port begins
        #i.e. the location of the last ":"
        portaddr_split=local_address.rfind(':')
        if portaddr_split == -1:
            logging.warn("Invalid IP address " + local_address)

        #construct identifier for client_sockets
        key=(local_address, peer, remote_address)
        #pretend socket is already connected in case data is recieved while connecting
        self.client_sockets[key] = asyncore.dispatcher(map=self.map)
        #just some asyncore initialization stuff
        self.client_sockets[key].buffer=b''
        self.client_sockets[key].writable=lambda: False
        self.client_sockets[key].readable=lambda: False

        logging.debug("connecting to "+local_address)

        try: # connect to the ip:port
            connected_socket=socket.create_connection((local_address[:portaddr_split], int(local_address[portaddr_split+1:])))
        except (socket.error, OverflowError, ValueError):
            logging.warning("could not connect to "+local_address)
            del(self.client_sockets[key])
            #if it could not connect, tell the bot on the the other side to disconnect
            self.sendMessageWrapper(peer, local_address, remote_address, "disconnect me!", 'chat')

        # attach the socket to the appropriate client_sockets and fix asyncore methods
        self.client_sockets[key].set_socket(connected_socket)
        self.client_sockets[key].connected = True
        self.client_sockets[key].socket.setblocking(0)
        self.client_sockets[key].writable=lambda: len(self.client_sockets[key].buffer)>0
        self.client_sockets[key].handle_write=lambda: self.handle_write(key)
        self.client_sockets[key].readable=lambda: True
        self.client_sockets[key].handle_read=lambda: self.handle_read(local_address, peer, remote_address)

    def add_client_socket(self, local_address, peer, remote_address, sock):
        """Add socket to the bot's routing table."""

        # Calculate the client_sockets identifier for this socket, and put in the dict.
        key=(local_address,peer,remote_address)
        self.client_sockets[key] = asyncore.dispatcher(sock, map=self.map)

        #just some asyncore initialization stuff
        self.client_sockets[key].buffer=b''
        self.client_sockets[key].writable=lambda: len(self.client_sockets[key].buffer)>0
        self.client_sockets[key].handle_write=lambda: self.handle_write(key)
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

    ### asyncore callbacks:

    def handle_read(self, local_address, peer, remote_address):
        """Called when a TCP socket has stuff to be read from it."""

        key = (local_address,peer,remote_address)
        data=base64.b64encode(self.client_sockets[key].recv(8192)).decode("UTF-8")

        #remember, you generally cannot send blank messages over xmpp
        #so blank messages are represented by a "_"
        if not data:
            data = "_"

        self.sendMessageWrapper(peer, local_address, remote_address, data, 'chat')

    def handle_write(self, key):
        """Called when a TCP socket has stuff to be written to it."""
        data=self.client_sockets[key].buffer
        self.client_sockets[key].buffer=self.client_sockets[key].buffer[self.client_sockets[key].send(data):]

    def handle_accept(self, local_address, peer, remote_address):
        """Called when we have a new incoming connection to one of our listening sockets."""

        connection, local_address = self.server_sockets[local_address].accept()
        local_address=local_address[0]+":"+str(local_address[1])
        #add the new connected socket to client_sockets
        self.add_client_socket(local_address, peer, remote_address, connection)
        #send a connection request to the bot waiting on the other side of the xmpp server
        self.sendMessageWrapper(peer, local_address, remote_address, 'connect me!', 'chat')

    def handle_close(self, key):
        """Called when the TCP client socket closes."""

        if key in self.client_sockets:
            self.client_sockets[key].close()
            del(self.client_sockets[key])
            local_address, peer, remote_address = key
            #send a disconnection request to the bot waiting on the other side of the xmpp server
            self.sendMessageWrapper(peer, local_address, remote_address, 'disconnect me!', 'chat')

    def handle_connect(self, client_socket, key):
        """Called when the TCP client socket connects successfully to destination."""

        local_address=key[0]
        logging.debug("connecting to "+local_address)
        client_socket.setblocking(0)
        #add the socket to bot's client_sockets
        self.client_sockets[key]=client_socket

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
