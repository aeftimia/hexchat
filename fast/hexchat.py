#!/usr/bin/env python
import asyncore, logging, socket, sleekxmpp, sys, base64

if sys.version_info < (3, 0):
    reload(sys)
    sys.setdefaultencoding('utf8')
else:
    raw_input = input

class sockbot(sleekxmpp.ClientXMPP):
    def __init__(self, jid, password):
        self.server_socks={}
        self.client_socks={}
        self.map = {}
        sleekxmpp.ClientXMPP.__init__(self, jid, password)
        
        if jid.find("gmail.com")!=-1:
            self.connect_address = ("talk.google.com", 5222)
        else:
            self.connect_address = None
            
        self.add_event_handler("session_start", self.session_start)
        #self.add_event_handler("disconnected", lambda x: self.connect(self.connect_address))
        self.add_event_handler("message", self.get_message)
        self.register_plugin('xep_0030') # Service Discovery
        self.register_plugin('xep_0045') # Multi-User Chat
        self.register_plugin('xep_0199') # XMPP Ping
        self.scheduler.add("asyncore loop", 0.001, asyncore.loop, (0.0, True, self.map, 1), repeat=True)

        if self.connect(self.connect_address):
            self.process()
        else:
            raise Exception(jid+" could not connect")

    def session_start(self, event):
        self.send_presence()

    def get_message(self, msg):
        print(msg['subject']+"<=="+msg['nick']['nick']+":"+msg['body'])
        key = msg['subject']+"==>"+msg['from'].bare+"==>"+msg['nick']['nick']
        if key in self.client_socks:
            if msg['body']=="_":
                self.client_socks[key].send(b'')
            elif msg['body']=="disconnect me!":
                self.handle_close(key)
            else:
                self.client_socks[key].send(base64.b64decode(msg['body'].encode("UTF-8")))
        elif msg['body']=='connect me!':
            sock=socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setblocking(0)
            portaddr_split=msg['subject'].rfind(':')
            sock.connect_ex((msg['subject'][:portaddr_split], int(msg['subject'][portaddr_split+1:])))
            self.add_socket(msg['subject'], msg['from'].bare, msg['nick']['nick'], sock)
        else:
            print('packet dropped')
            #if msg['body'] not in ("disconnect me!", "_"):
            #    self.sendMessageWrapper(msg['from'].bare, msg['subject'], msg['nick']['nick'], "disconnect me!", 'chat')

    def handle_read(self, local_address, peer, remote_address):
        key = local_address+"==>"+peer+"==>"+remote_address
        data=base64.b64encode(self.client_socks[key].recv(8192)).decode("UTF-8")
        if data:
            self.sendMessageWrapper(peer, local_address, remote_address, data, 'chat')
        else:
            self.sendMessageWrapper(peer, local_address, remote_address, "_", 'chat')

    def handle_accept(self, local_address, peer, remote_address):
        connection, local_address = self.server_socks[local_address].accept()
        local_address=local_address[0]+":"+str(local_address[1])
        self.add_socket(local_address, peer, remote_address, connection)
        self.sendMessageWrapper(peer, local_address, remote_address, 'connect me!', 'chat')

    def handle_close(self, key):
        if key in self.client_socks:
            self.client_socks[key].close()
            del(self.client_socks[key])
            local_address, peer, remote_address = key.split("==>")
            self.sendMessageWrapper(peer, local_address, remote_address, 'disconnect me!', 'chat')

    def sendMessageWrapper(self, mto0, mnick0, msubject0, mbody0, mtype0):
        print(mnick0+"==>"+msubject0+":"+mbody0)
        self.sendMessage(mto=mto0, mnick=mnick0, msubject=msubject0, mbody=mbody0, mtype=mtype0)
    
    def add_socket(self, local_address, peer, remote_address, sock=None):
        if sock != None:
            key=local_address+"==>"+peer+"==>"+remote_address
            self.client_socks[key] = asyncore.dispatcher(sock, map=self.map)
            self.client_socks[key].writable=lambda: False
            self.client_socks[key].handle_read=lambda: self.handle_read(local_address, peer, remote_address)
            self.client_socks[key].handle_close=lambda: self.handle_close(key)
        else:
            self.server_socks[local_address] = asyncore.dispatcher(map=self.map)
            self.server_socks[local_address].create_socket(socket.AF_INET, socket.SOCK_STREAM)
            self.server_socks[local_address].writable=lambda: False
            self.server_socks[local_address].set_reuse_addr()
            portaddr_split=local_address.rfind(':')
            self.server_socks[local_address].bind((local_address[:portaddr_split], int(local_address[portaddr_split+1:])))
            self.server_socks[local_address].handle_accept = lambda: self.handle_accept(local_address, peer, remote_address)
            self.server_socks[local_address].listen(1023)

if __name__ == '__main__':
    #logging.basicConfig(level=1, format='%(levelname)-8s %(message)s')
    bots={}
    lines=open(sys.argv[1]).read().splitlines()
    for line in lines:
        if line[-1]==":":
            userpass_split=line.find(':')
            username=line[:userpass_split]
            bots[username]=sockbot(username, line[userpass_split+1:-1])
            continue
        [local_address, peer, remote_address]=line.split('==>')           
        bots[username].add_socket(local_address, peer, remote_address)
    #asyncore.loop(0.05)
