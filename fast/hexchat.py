#!/usr/bin/env python
import asyncore, logging, socket, sleekxmpp, sys, base64, pyasn1_modules

if sys.version_info < (3, 0):
    reload(sys)
    sys.setdefaultencoding('utf8')
else:
    raw_input = input

#this class exchanges data between tcp sockets and xmpp servers.
class bot(sleekxmpp.ClientXMPP):
    #jid is the login username@chatserver
    #password is the password to login with 
    def __init__(self, jid, password):
        #server sockets is a like a routing table
        #that maps a local IP address that listens for tcp connections
        #to a path the traffic should take through the xmpp server
        self.server_sockets={}
        #client sockets is like a routing table
        #that maps a connected tcp socket
        #to a path the traffic should take through an xmpp server
        #the keys are tuples of the form:
        #(bound ip:port on client, xmpp username of server, ip:port server should forward data to)
        #and the values are connected sockets.
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

        if self.connect(self.connect_address):
            self.process()
        else:
            raise(Exception(jid+" could not connect"))

    def session_start(self, event):
        self.send_presence()

    def disconnected(self, event):
        logging.warn("XMPP chat server disconnected")
        logging.debug("Trying to reconnect")
        if self.connect(self.connect_address):
            logging.debug("connection reestabilshed")
        else:
            raise(Exception(jid+" could not connect"))

    #get_message evaluates filters incomming xmpp messages
    #and directs them to the proper socket
    def get_message(self, msg):
        #print a debug message that notifies the user of incomming data
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
                    sock.connect_ex((msg['subject'][:portaddr_split], int(msg['subject'][portaddr_split+1:])))
                    #add the socket to bot's client_sockets
                    self.add_socket(msg['subject'], msg['from'].bare, msg['nick']['nick'], sock)
                except socket.error as msg:
                    logging.debug(msg)
                    #if it could not connect, tell the bot on the the other side to disconnect
                    self.sendMessageWrapper(msg['from'].bare, msg['subject'], msg['nick']['nick'], "disconnect me!", 'chat')
                
        #else:
        #The key was not found in the client_sockets routing table.
        else:
            #Dropped packets seems to be the biggest bottleneck in this connection
            #Since the sockets are using tcp, they think the other party has sent and recieved data
            #by the time the message is piped to the xmpp server.
            #The problem is if one party disconnects while the other sends a message, the packet has to be dropped
            #This leads to some degree of unreliability
            #It seems that without raw sockets, this bottleneck is an inherent flaw in the xmpp tunnel design.
            logging.warn('packet dropped')
            #if msg['body'] not in ("disconnect me!", "_"):
            #    self.sendMessageWrapper(msg['from'].bare, msg['subject'], msg['nick']['nick'], "disconnect me!", 'chat')

    #this is the function that gets called when a tcp socket is ready to be read
    def handle_read(self, local_address, peer, remote_address):
        key = (local_address,peer,remote_address)
        data=base64.b64encode(self.client_sockets[key].recv(8192)).decode("UTF-8")
        #remember, you generally cannot send blank messages over xmpp
        #so blank messages are represented by a "_"
        if data:
            self.sendMessageWrapper(peer, local_address, remote_address, data, 'chat')
        else:
            self.sendMessageWrapper(peer, local_address, remote_address, "_", 'chat')

    #this is the function that gets called when a tcp server socket gets a request
    #to accept a connection
    def handle_accept(self, local_address, peer, remote_address):
        connection, local_address = self.server_sockets[local_address].accept()
        local_address=local_address[0]+":"+str(local_address[1])
        #add the new connected socket to client_sockets
        self.add_socket(local_address, peer, remote_address, connection)
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

    #this just sends a message using sleekxmpp's sendMessage function
    #It also prints a debug message indicating data being sent
    def sendMessageWrapper(self, mto0, mnick0, msubject0, mbody0, mtype0):
        #logging.debug(mnick0+"==>"+msubject0+":"+mbody0)
        self.sendMessage(mto=mto0, mnick=mnick0, msubject=msubject0, mbody=mbody0, mtype=mtype0)

    #this adds a socket to the bot's routing table
    def add_socket(self, local_address, peer, remote_address, sock=None):
        #if a connected socket, sock, is supplied, add it to the client_sockets routing table
        if sock != None:
            #key is the key to the client_sockets routing table
            #it consists of a local_address, a peer (the username@chatserver of the other party), and a remote_address
            #keep in mind, the remote address is the address read by the bot at the other end
            #so it is probably going to be 127.0.0.1, not an external ip address
            #(unless you want the remote computer to connect to some external ip address)
            key=(local_address,peer,remote_address)
            self.client_sockets[key] = asyncore.dispatcher(sock, map=self.map)
            #just some asyncore initialization stuff
            self.client_sockets[key].writable=lambda: False
            self.client_sockets[key].handle_read=lambda: self.handle_read(local_address, peer, remote_address)
            self.client_sockets[key].handle_close=lambda: self.handle_close(key)
        #if no sock is supplied, 
        #it must be a server socket listening for connections
        else:
            portaddr_split=local_address.rfind(':')
            if portaddr_split!=-1:
                self.server_sockets[local_address] = asyncore.dispatcher(map=self.map)
                #just some asyncore initialization stuff
                self.server_sockets[local_address].create_socket(socket.AF_INET, socket.SOCK_STREAM)
                self.server_sockets[local_address].writable=lambda: False
                self.server_sockets[local_address].set_reuse_addr()
                self.server_sockets[local_address].bind((local_address[:portaddr_split], int(local_address[portaddr_split+1:])))
                self.server_sockets[local_address].handle_accept = lambda: self.handle_accept(local_address, peer, remote_address)
                self.server_sockets[local_address].listen(1023)
            else:
                raise(Exception("No port specified"))

if __name__ == '__main__':
    logging.basicConfig(filename=sys.argv[2],level=logging.DEBUG)
    if sys.argv[1]=="-c":
        if not len(sys.argv) in (5,10):
            raise(Exception("Wrong number of command line arguements"))
        else:
            username=sys.argv[3]
            password=sys.argv[4] 
            bot0=bot(username, password)
            if len(sys.argv)==10:
                bot0.add_socket(sys.argv[5]+":"+sys.argv[6], sys.argv[7], sys.argv[8]+":"+sys.argv[9])
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
                bots[username].add_socket(local_address, peer, remote_address)
            except socket.error as msg:
                raise(msg)
