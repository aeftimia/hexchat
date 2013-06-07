#!/usr/bin/env python
import asyncore, logging, socket, sleekxmpp, sys, base64
from Crypto.Cipher import AES
from Crypto import Random

if sys.version_info < (3, 0):
    reload(sys)
    sys.setdefaultencoding('utf8')
else:
    raw_input = input

BS = 16
pad = lambda s: s + (BS - len(s) % BS) * chr(BS - len(s) % BS) 
unpad = lambda s : s[0:-s[-1]]

class AESCipher:
    def __init__( self, key ):
        self.key = key.encode("UTF-8")
        self.key+="=".encode("UTF-8")*(32-len(self.key)) 

    def encrypt( self, raw ):
        raw = pad(base64.b64encode(raw).decode("UTF-8"))
        iv = Random.new().read( AES.block_size )
        cipher = AES.new( self.key, AES.MODE_CBC, iv )
        return base64.b64encode( iv + cipher.encrypt( raw ) ).decode("UTF-8") 

    def decrypt( self, enc ):
        enc = base64.b64decode(enc.encode("UTF-8") )
        iv = enc[:16]
        cipher = AES.new(self.key, AES.MODE_CBC, iv )
        return base64.b64decode(unpad(cipher.decrypt( enc[16:] )))

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
        self.scheduler.add("asyncore loop", 0.01, asyncore.loop, (0.01, True, self.map, 1), repeat=True)

        if self.connect(self.connect_address):
            self.process()
        else:
            raise Exception(jid+" could not connect")

    def session_start(self, event):
        self.send_presence()

    def get_message(self, msg0):
        print(msg0['subject']+"<=="+msg0['nick']['nick']+":"+msg0['body'])
        for key0 in frozenset(self.client_socks):
            try:
                msub = self.client_socks[key0].cipher.decrypt(msg0['subject']).decode("UTF-8")
                mnick = self.client_socks[key0].cipher.decrypt(msg0['nick']['nick']).decode("UTF-8")
                key = msub+"==>"+msg0['from'].bare+"==>"+mnick
                if key == key0:
                    mbody = self.client_socks[key0].cipher.decrypt(msg0['body'])
                    if mbody=="_".encode("UTF-8"):
                        self.client_socks[key].send(b'')
                    elif mbody=="disconnect me!".encode("UTF-8"):
                        self.handle_close(key)
                    else:
                        self.client_socks[key].send(mbody)
                    return()    
            except:
                pass

        try:
            mbody = self.default_cipher.decrypt(msg0['body'])
            if mbody=='connect me!'.encode("UTF-8"):
                msub = self.default_cipher.decrypt(msg0['subject']).decode("UTF-8")
                mnick = self.default_cipher.decrypt(msg0['nick']['nick']).decode("UTF-8")
                sock=socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.setblocking(0)
                portaddr_split=msub.rfind(':')
                try:
                    sock.connect_ex((msub[:portaddr_split], int(msub[portaddr_split+1:])))
                    self.add_socket(msub, msg0['from'].bare, mnick, self.default_cipher, sock)
                except:
                    del(sock)
                    self.sendMessageWrapper(msg0['from'].bare, msub, mnick, "disconnect me!".encode("UTF-8"), "chat")
                return()
        except:
            print("packet dropped")

    def handle_read(self, local_address, peer, remote_address):
        key = local_address+"==>"+peer+"==>"+remote_address
        data=self.client_socks[key].recv(8192)
        if data:
            self.sendMessageWrapper(peer, local_address, remote_address, data, 'chat')
        else:
            self.sendMessageWrapper(peer, local_address, remote_address, "_".encode("UTF-8"), 'chat')

    def handle_accept(self, local_address0, peer, remote_address):
        connection, local_address = self.server_socks[local_address0].accept()
        local_address=local_address[0]+":"+str(local_address[1])
        self.add_socket(local_address, peer, remote_address, self.server_socks[local_address0].cipher, connection)
        self.sendMessageWrapper(peer, local_address, remote_address, 'connect me!'.encode("UTF-8"), 'chat')

    def handle_close(self, key):
        if key in self.client_socks:
            self.client_socks[key].close()
            del(self.client_socks[key])
            local_address, peer, remote_address = key.split("==>")
            self.sendMessageWrapper(peer, local_address, remote_address, 'disconnect me!'.encode("UTF-8"), 'chat')

    def sendMessageWrapper(self, mto0, mnick0, msubject0, mbody0, mtype0):
        print(mnick0+"==>"+msubject0+":"+base64.b64encode(mbody0).decode("UTF-8"))
        key=mnick0+"==>"+mto0+"==>"+msubject0
        if key in self.client_socks:
            cipher=self.client_socks[key].cipher
        else:
            cipher=self.default_cipher
        mnick0=cipher.encrypt(mnick0.encode("UTF-8"))
        msubject0=cipher.encrypt(msubject0.encode("UTF-8"))
        mbody0=cipher.encrypt(mbody0)        
        self.sendMessage(mto=mto0, mnick=mnick0, msubject=msubject0, mbody=mbody0, mtype=mtype0)
    
    def add_socket(self, local_address, peer, remote_address, cipher, sock=None):
        if sock != None:
            key=local_address+"==>"+peer+"==>"+remote_address
            self.client_socks[key] = asyncore.dispatcher(sock, map=self.map)
            self.client_socks[key].cipher=cipher
            self.client_socks[key].writable=lambda: False
            self.client_socks[key].handle_read=lambda: self.handle_read(local_address, peer, remote_address)
            self.client_socks[key].handle_close=lambda: self.handle_close(key)
        else:
            self.server_socks[local_address] = asyncore.dispatcher(map=self.map)
            self.server_socks[local_address].cipher=cipher
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
    peer_ciphers={}
    lines=open(sys.argv[1]).read().splitlines()
    password_part=True
    for line in lines:
        if password_part:
            if line=="==End Passwords==":
                password_part=False
                continue
            jidpass_split=line.find(':')
            peer_ciphers[line[:jidpass_split]]=AESCipher(line[jidpass_split+1:])
        else:
            if line[-1]==":":
                userpass_split=line.find(':')
                username=line[:userpass_split]
                bots[username]=sockbot(username, line[userpass_split+1:-1])
                if username in peer_ciphers:
                    bots[username].default_cipher=peer_ciphers[username]
                continue
            [local_address, peer, remote_address]=line.split('==>')           
            bots[username].add_socket(local_address, peer, remote_address, peer_ciphers[peer])
    #asyncore.loop(0.05)
