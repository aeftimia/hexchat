import asyncore
import base64
import logging
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
        self.running_lock=threading.RLock()
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

    #check client sockets for buffered data
    def read_socket(self):
        while True:
            data=self.recv(RECV_RATE)
            if data:
                #start a new thread because sleekxmpp uses an RLock for blocking sends
                threading.Thread(name='%d sending data %d' % (hash(self.key), self.id), target=lambda: self.master.send_data(self.key, base64.b64encode(data).decode("UTF-8"), self.id, self.get_alias(), self.get_bot())).start()
                with self.id_lock:
                    self.id=(self.id+1)%MAX_ID
            else:
                with self.running_lock:
                    if self.running:
                        self.master.send_disconnect(self.key, self.id, self.get_alias(), self.get_bot())
                        self._handle_close()
                return
            time.sleep(THROTTLE_RATE/float(len(self.master.bots)))

    def buffer_message(self, iq_id, data):
        threading.Thread(name="%d buffer message %d" % (hash(self.key), iq_id), target=lambda: self.buffer_message_thread(iq_id, data)).start()
            
    def buffer_message_thread(self, iq_id, data):
        with self.running_lock:
            if not self.running:
                return
                
            raw_id_diff=(iq_id-self.last_id_received)
            id_diff=raw_id_diff%MAX_ID
            if raw_id_diff<0 and raw_id_diff>-MAX_ID/2. or id_diff>MAX_ID_DIFF:
                logging.warn("received redundant message or too many messages in buffer. Disconnecting")
                self.master.send_disconnect(self.key, self.id, self.get_alias(), self.get_bot())
                self._handle_close()
                return

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
                    data=data[self.send(data):]

    def _handle_close(self):
        """Called when the TCP client socket closes."""
        with self.running_lock:
            if not self.running:
                return
            self.running=False
            (local_address, remote_address)=(self.key[0], self.key[2])
            logging.debug("disconnecting %s:%d from " % local_address +  "%s:%d" % remote_address)
            self.close()
            self.master.delete_socket(self.key)

    def close(self):
        #self.connected = False
        #self.accepting = False
        #self.del_channel()
        try:
            self.socket.close()
        except socket.error as why:
            if why.args[0] not in (asyncore.ENOTCONN, asyncore.EBADF):
                raise

