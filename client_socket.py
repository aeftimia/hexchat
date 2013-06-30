import asyncore
import base64
import logging
import socket
import time
import threading

RECV_RATE=2**15 #bytes
MAX_ID=2**32-1
MAX_ID_DIFF=2**10
THROTTLE_RATE=0.1


class client_socket():
    def __init__(self, master, key, socket):
        self.master=master
        self.key=key
        self.running=True
        self.running_lock=threading.Lock()
        self.reading=True
        self.reading_lock=threading.Lock()
        self.writing=True
        self.incomming_messages=[]
        self.last_id_received=0
        self.writing_lock=threading.Lock()
        self.aliases=list(self.key[1])
        self.alias_index=0
        self.alias_lock=threading.Lock()
        self.id=0
        self.id_lock=threading.Lock()
        socket.setblocking(1)
        self.socket=socket

    def run(self):
        threading.Thread(name="read socket %d" % hash(self.key), target=lambda: self.read_socket()).start()

    def get_alias(self):
        with self.alias_lock:
            alias=self.aliases[self.alias_index%len(self.aliases)]
            self.alias_index=(self.alias_index+1)%len(self.aliases)
            return alias

    def get_id(self):
        with self.id_lock:
            iq_id=self.id
            self.id=(self.id+1)%MAX_ID
            return iq_id

    #check client sockets for buffered data
    def read_socket(self):
        while True:
            with self.reading_lock:
                if not self.reading:
                    return
                    
                then=time.time()
                data=self.recv(RECV_RATE)
                if data:
                    self.send_message(data)
                else:
                    self.stop_writing()
                    self.handle_close(True)
                    return
                    
            dtime=time.time()-then
            if dtime<THROTTLE_RATE:
                time.sleep(THROTTLE_RATE-dtime)

    def send_message(self, data):
        iq_id=self.get_id()
        str_data=base64.b64encode(data).decode("UTF-8")
        self.master.send_data(self.key, str_data, iq_id, self.get_alias())
        
    def buffer_message(self, iq_id, data):
        threading.Thread(name="%d buffer message %d" % (hash(self.key), iq_id), target=lambda: self.buffer_message_thread(iq_id, data)).start()
        self.master.client_sockets_lock.release()
            
    def buffer_message_thread(self, iq_id, data):
        if data=="disconnect": #no need for validation since a bad ID leads to the same result
            self.stop_reading()
                
        with self.writing_lock:     
            if not self.writing:
                return
                           
            raw_id_diff=iq_id-self.last_id_received
            id_diff=raw_id_diff%MAX_ID
            if raw_id_diff<0 and raw_id_diff>-MAX_ID/2. or id_diff>MAX_ID_DIFF:
                logging.warn("received redundant message or too many messages in buffer. Disconnecting")
                self.stop_reading()
                self.writing=False
                self.handle_close(True)
                return

            logging.debug("%s:%d received data from " % self.key[0] + "%s:%d" % self.key[2])

            #place None in empty buffer elements
            while id_diff>=len(self.incomming_messages):
                self.incomming_messages.append(None)

            self.incomming_messages[id_diff]=data
            logging.debug("%s:%d looking for id:"%self.key[0]+str(self.last_id_received))

            while self.incomming_messages and self.incomming_messages[0]!=None:
                data=self.incomming_messages.pop(0)
                if data=="disconnect":
                    self.writing=False
                    self.handle_close()
                    return
                self.last_id_received=(self.last_id_received+1)%MAX_ID
                logging.debug("%s:%d now looking for id:"%self.key[0]+str(self.last_id_received))
                while data:   
                    bytes=self.send(data)
                    if bytes==None:
                        self.stop_reading()
                        self.writing=False
                        self.handle_close(True)
                        return
                    data=data[bytes:]

    def stop_reading(self):
        threading.Thread(name="stop reading %d"%hash(self.key), target=lambda: self.stop_reading_thread()).start()
        
    def stop_reading_thread(self):
        with self.reading_lock:
            self.reading=False

    def stop_writing(self):
        threading.Thread(name="stop writing %d"%hash(self.key), target=lambda: self.stop_writing_thread()).start()

    def stop_writing_thread(self):
        with self.writing_lock:
            self.writing=False

    def handle_close(self, send_disconnect=False):
        """Called when the TCP client socket closes."""
        with self.running_lock:
            if not self.running:
                return
            self.running=False
            (local_address, remote_address)=(self.key[0], self.key[2])
            logging.debug("disconnecting %s:%d from " % local_address +  "%s:%d" % remote_address)
            self.close()
            with self.master.client_sockets_lock:
                if self.key in self.master.client_sockets:
                    self.master.delete_socket(self.key)
                    if send_disconnect:
                        self.master.send_disconnect(self.key, self.get_id(), self.get_alias())

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
        try:
            self.socket.close()
        except socket.error as why:
            if why.args[0] not in (asyncore.ENOTCONN, asyncore.EBADF):
                raise
