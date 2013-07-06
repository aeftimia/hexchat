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
    def __init__(self, master, key, from_aliases, socket):
        self.master=master
        self.key=key
        self.write_buffer=b''
        self.to_aliases=list(self.key[1])
        self.to_alias_index=0
        self.to_alias_lock=threading.Lock()
        self.from_aliases=from_aliases
        self.id=0
        self.last_id_received=0
        self.incomming_message_db={}
        self.reading=True
        socket.setblocking(0)
        self.socket=socket

    def get_to_alias(self):
        with self.to_alias_lock:
            to_alias=self.to_aliases[self.to_alias_index]
            self.to_alias_index=(self.to_alias_index+1)%len(self.to_aliases)
            return to_alias

    def get_from_aliases(self):
        return self.from_aliases

    def get_id(self):
        iq_id=self.id
        self.id=(self.id+1)%MAX_ID
        return iq_id

    def send_message(self, data):
        iq_id=self.get_id()
        str_data=base64.b64encode(data).decode("UTF-8")
        self.master.send_data(self.key, self.from_aliases, str_data, self.get_to_alias(), iq_id)
        
    def buffer_message(self, iq_id, data):
        if data=="disconnect":
            self.reading=False
                        
        raw_id_diff=iq_id-self.last_id_received
        id_diff=raw_id_diff%MAX_ID
                        
        if raw_id_diff<0 and raw_id_diff>-MAX_ID/2. or iq_id in self.incomming_message_db or sys.getsizeof(self.incomming_message_db)>=MAX_DB_SIZE:
                logging.warn("received redundant message or too many messages in buffer. Disconnecting")
                self.handle_close(True)
                return

        if data=="disconnect":
            logging.debug("%s:%d " % self.key[0] + "received disconnect from %s:%d" % self.key[2]+ " with id:%d" % iq_id)
        else:
            logging.debug("%s:%d " % self.key[0] + "received %d bytes from " % len(data)+ "%s:%d" % self.key[2]+ " with id:%d" % iq_id)
            logging.debug("%s:%d looking for id:"%self.key[0]+str(self.last_id_received))

        self.incomming_message_db[iq_id]=data
        while self.last_id_received in self.incomming_message_db:
            data=self.incomming_message_db.pop(self.last_id_received)
            if data=="disconnect":
                self.handle_close()
                self.master.client_sockets_lock.release()
                return
            self.last_id_received=(self.last_id_received+1)%MAX_ID
            logging.debug("%s:%d now looking for id:"%self.key[0]+str(self.last_id_received))
            self.write_buffer+=data
        
        self.master.client_sockets_lock.release()

    def handle_close(self, send_disconnect=False):
        """Called when the TCP client socket closes."""
        (local_address, remote_address)=(self.key[0], self.key[2])
        logging.debug("disconnecting %s:%d from " % local_address +  "%s:%d" % remote_address)
        self.close()
        
        self.master.delete_socket(self.key)
         
        for bot_index in self.from_aliases:
            with self.master.bots[bot_index].num_clients_lock:
                self.master.bots[bot_index].num_clients-=1 
                    
        if send_disconnect:
            self.master.send_disconnect(self.key, self.from_aliases, self.get_to_alias(), self.get_id())
            with self.master.pending_disconnects_lock: #wait for an error from the chat server
                to_aliases=set(self.to_aliases)
                self.master.pending_disconnects[self.key]=(self.from_aliases, to_aliases)
                self.master.pending_disconnect_timeout(self.key, to_aliases)

    #socket methods
    def send(self, data):
        try:
            result = self.socket.send(data)
            return result
        except socket.error as why:
            if why.args[0] in asyncore._DISCONNECTED:
                self.handle_close(True)
                return 0
            else:
                raise

    def recv(self):
        try:
            data = self.socket.recv(MAX_SIZE)
            if not data:
                # a closed connection is indicated by signaling
                # a read condition, and having recv() return 0.
                self.handle_close(True)
            else:
                self.send_message(data)
        except socket.error as why:
            # winsock sometimes throws ENOTCONN
            if why.args[0] in asyncore._DISCONNECTED:
                self.handle_close(True)
            else:
                raise

    def close(self):
        try:
            self.socket.close()
        except socket.error as why:
            if why.args[0] not in (asyncore.ENOTCONN, asyncore.EBADF):
                raise
