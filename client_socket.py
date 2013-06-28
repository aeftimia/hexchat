import asyncore
import base64
import logging
import socket
import time
import threading

RECV_RATE=2**15
THROTTLE_RATE=1.0
MAX_ID=2**32-1
MAX_ID_DIFF=100

class client_socket(asyncore.dispatcher):
    def __init__(self, master, key, socket):
        self.master=master
        self.key=key
        self.aliases=list(key[1])
        self.id=0
        self.last_id_received=0
        self.incomming_messages=[]
        self.alias_index=0
        self.buffer=b''
        self.running=True
        self.reading=True
        self.running_lock=threading.Lock()
        self.write_lock=threading.Lock()
        self.alias_lock=threading.Lock()
        self.id_lock=threading.Lock()
        self.bot_lock=threading.Lock()
        self.bot_index=0
        socket.setblocking(1)
        self.socket=socket

    def run(self):
        threading.Thread(name="read socket %d" % hash(self.key), target=lambda: self.read_socket()).start()

    def get_alias(self):
        with self.alias_lock:
            alias=self.aliases[self.alias_index%len(self.aliases)]
            self.alias_index=(self.alias_index+1)%len(self.aliases)
            return alias

    def get_bot(self):
        with self.bot_lock:
            bot=self.master.bots[self.bot_index%len(self.master.bots)]
            self.bot_index=(self.bot_index+1)%len(self.master.bots)
            return bot

    def get_id(self):
        with self.id_lock:
            iq_id=self.id
            self.id=(self.id+1)%MAX_ID
            return iq_id

    #check client sockets for buffered data
    def read_socket(self):
        while self.reading:
            data=self.recv(RECV_RATE)
            
            if not self.reading:
                return
                
            if data:
                #start a new thread because sleekxmpp uses an RLock for blocking sends
                self.master.send_data(self.key, base64.b64encode(data).decode("UTF-8"), self.get_id(), self.get_alias(), self.get_bot())
            else:
                self._handle_close(True)
                return
            time.sleep(THROTTLE_RATE/len(self.master.bots))

    def buffer_message(self, iq_id, data):
        threading.Thread(name="%d buffer message %d" % (hash(self.key), iq_id), target=lambda: self.buffer_message_thread(iq_id, data)).start()
            
    def buffer_message_thread(self, iq_id, data):
        with self.write_lock:                
            raw_id_diff=(iq_id-self.last_id_received)
            id_diff=raw_id_diff%MAX_ID
            if raw_id_diff<0 and raw_id_diff>-MAX_ID/2. or id_diff>MAX_ID_DIFF:
                logging.warn("received redundant message or too many messages in buffer. Disconnecting")
                self._handle_close(True)
                return

            #stop the socket from reading more data
            if data=="disconnect":
                self.reading=False

            logging.debug("%s:%d received data from " % self.key[0] + "%s:%d" % self.key[2])
            while id_diff>=len(self.incomming_messages):
                self.incomming_messages.append(None)

            self.incomming_messages[id_diff]=data
            logging.debug("%s:%d looking for id:"%self.key[0]+str(self.last_id_received))
            while self.incomming_messages and self.incomming_messages[0]!=None:
                data=self.incomming_messages.pop(0)
                if data=="disconnect":
                    self._handle_close()
                    return
                self.last_id_received=(self.last_id_received+1)%MAX_ID
                logging.debug("%s:%d now looking for id:"%self.key[0]+str(self.last_id_received))
                while data:   
                    bytes=self.send(data)
                    if bytes==None:
                        self._handle_close()
                        return
                    data=data[bytes:]

    def _handle_close(self, send_disconnect=False):
        """Called when the TCP client socket closes."""
        with self.running_lock:
            self.__handle_close(send_disconnect)

    def __handle_close(self, send_disconnect=False):
        if not self.running:
            return
        self.running=False
        (local_address, remote_address)=(self.key[0], self.key[2])
        logging.debug("disconnecting %s:%d from " % local_address +  "%s:%d" % remote_address)
        self.close()
        self.master.delete_socket(self.key)
        if send_disconnect:
            self.master.send_disconnect(self.key, self.get_id(), self.get_alias(), self.get_bot())

    #overwrites of asyncore methods
    def send(self, data):
        try:
            result = self.socket.send(data)
            return result
        except socket.error as why:
            if why.args[0] in asyncore._DISCONNECTED:
                return None
            else:
                raise

    def recv(self, buffer_size):
        try:
            data = self.socket.recv(buffer_size)
            if not data:
                # a closed connection is indicated by signaling
                # a read condition, and having recv() return 0.
                return b''
            else:
                return data
        except socket.error as why:
            # winsock sometimes throws ENOTCONN
            if why.args[0] in asyncore._DISCONNECTED:
                return b''
            else:
                raise

    def close(self):
        #self.connected = False
        #self.accepting = False
        #self.del_channel()
        try:
            self.socket.close()
        except socket.error as why:
            if why.args[0] not in (asyncore.ENOTCONN, asyncore.EBADF):
                raise

