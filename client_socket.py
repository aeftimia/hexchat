import asyncore
import base64
import logging
import socket
import threading
import sys

if sys.version_info < (3, 0):
    import Queue as queue
else:
    import queue

MAX_ID=2**32-1
MAX_DB_SIZE=2**22 #bytes
MAX_SIZE=2**15 #bytes

class client_socket():
    def __init__(self, master, key, socket):
        self.master=master
        self.key=key
        self.running=True
        self.running_lock=threading.Lock()
        self.reading=True
        self.reading_lock=threading.Lock()
        self.writing=True
        self.writing_lock=threading.Lock()
        self.incomming_message_queue=queue.Queue()
        self.aliases=list(self.key[1])
        self.alias_index=0
        self.id=0
        socket.setblocking(1)
        self.socket=socket

    def run(self):
        threading.Thread(name="check incomming message queue %d" % hash(self.key), target=lambda: self.check_incomming_message_queue()).start()
        threading.Thread(name="read socket %d" % hash(self.key), target=lambda: self.read_socket()).start()

    def get_alias(self):
        alias=self.aliases[self.alias_index%len(self.aliases)]
        self.alias_index=(self.alias_index+1)%len(self.aliases)
        return alias

    def get_id(self):
        iq_id=self.id
        self.id=(self.id+1)%MAX_ID
        return iq_id

    #check client sockets for buffered data
    def read_socket(self):
        while True:
            with self.reading_lock:
                if not self.reading:
                    return
                    
            data=self.recv(MAX_SIZE)
                
            with self.reading_lock:
                if not self.reading:
                    return
                                        
                if data:
                    self.send_message(data)
                else:
                    self.stop_writing()
                    self.handle_close(True)
                    return

    def send_message(self, data):
        iq_id=self.get_id()
        str_data=base64.b64encode(data).decode("UTF-8")
        self.master.send_data(self.key, str_data, self.get_alias(), iq_id)
        
    def buffer_message(self, iq_id, data):
        self.master.client_sockets_lock.release()
        if data=="disconnect": #no need for validation since a bad ID leads to the same result
            self.stop_reading()
        self.incomming_message_queue.put((iq_id, data))

    def check_incomming_message_queue(self):
        incomming_message_db={}
        last_id_received=0
        while True:
            (iq_id, data)=self.incomming_message_queue.get()
            if data==None:
                return
            
            raw_id_diff=iq_id-last_id_received
            id_diff=raw_id_diff%MAX_ID
                        
            with self.writing_lock:
                if not self.writing:
                    return
                
                if raw_id_diff<0 and raw_id_diff>-MAX_ID/2. or iq_id in incomming_message_db or sys.getsizeof(incomming_message_db)>=MAX_DB_SIZE:
                    logging.warn("received redundant message or too many messages in buffer. Disconnecting")
                    self.stop_reading()
                    self.handle_close(True)
                    return

                logging.debug("%s:%d " % self.key[0] + "received %d bytes from " % len(data)+ "%s:%d" % self.key[2]+ " with id:%d" % iq_id)
                logging.debug("%s:%d looking for id:"%self.key[0]+str(last_id_received))

                incomming_message_db[iq_id]=data
                while last_id_received in incomming_message_db:
                    data=incomming_message_db.pop(last_id_received)
                    if data=="disconnect":
                        self.handle_close()
                        return
                    last_id_received=(last_id_received+1)%MAX_ID
                    logging.debug("%s:%d now looking for id:"%self.key[0]+str(last_id_received))
                    while data:   
                        bytes=self.send(data)
                        if bytes==None:
                            self.stop_reading()
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
            self.incomming_message_queue.put((None, None)) #kill the thread that checks the queue

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
                        self.master.send_disconnect(self.key, self.get_alias(), self.get_id())
                        with self.master.pending_disconnects_lock: #wait for an error from the chat server
                            self.master.pending_disconnects[self.key]=self.key[1]
                            self.master.pending_disconnect_timeout(self.key, self.key[1])

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
