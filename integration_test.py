import argparse
import socket
import select
import threading
import time
import logging

from master import master
from stanza_plugins import register_stanza_plugins
import sys

from sleekxmpp.util import QueueEmpty

if sys.version_info < (3, 0):
    reload(sys)
    sys.setdefaultencoding('utf8')
else:
    raw_input = input

START_SEND_RATE=5*10**3//8
END_SEND_RATE=100*10**3//8
SEND_RATE_INCREMENT=10**3//8

SEND_DONE=threading.Event()
RECV_DONE=threading.Event()

def send(sock, master_):
    while True: #wait for connection
        with master_.client_sockets_lock:
            if master_.client_sockets:
                break
        time.sleep(1) 
        
    for datum in range(START_SEND_RATE, END_SEND_RATE, SEND_RATE_INCREMENT):
        str_datum=str(datum)
        str_datum='0'*(8-len(str_datum))+str_datum
        data=str_datum.encode("UTF-8")*datum
        then=time.time()
        
        #split the data into two chunks
        #sleeping for 1 second in between sending them
        #this garantees the data is not all sent instantly
        first_chunk_size=len(data)//2
        first_chunk_size=first_chunk_size-first_chunk_size%8 #send everything in 8 byte chunks
        first_chunk=data[:first_chunk_size]
        while first_chunk:
            bytes=sock.send(first_chunk)
            first_chunk=first_chunk[bytes:]
            
        time.sleep(1)
            
        second_chunk=data[first_chunk_size:]
        while second_chunk:
            bytes=sock.send(second_chunk)
            second_chunk=second_chunk[bytes:]
        
        
        SEND_DONE.set() #tell the recv thread to start looking for data
        
        num_kbytes=8*datum/1000
        send_rate=num_kbytes/(time.time()-then)   
        logging.warn("sent %fkb at %fkb/s" % (num_kbytes, send_rate))
        
        RECV_DONE.wait() #wait for the recv thread to get data
        RECV_DONE.clear()
        
    sock.close()

def recv(sock):
    datum=START_SEND_RATE
    best_recv_rate=0
    while datum<END_SEND_RATE:
        SEND_DONE.wait() #wait for data to be sent
        SEND_DONE.clear()
        num_bytes=8*datum
        num_kbytes=num_bytes/1000
        counter=0
        then=time.time()
        while counter<datum:
            data=sock.recv(num_bytes)
            while data:
                received_datum=int(data[:8])
                assert received_datum==datum
                counter+=1
                data=data[8:]
        sock.setblocking(0)
        assert not sock in select.select([sock],[],[], 0.0) #assert we are done reading
        sock.setblocking(1)        
        recv_rate=num_kbytes/(time.time()-then)
        logging.warn("received %fkb at %fkb/s" % (num_kbytes, recv_rate))
        if recv_rate>best_recv_rate:
            logging.warn("best recv rate is %fkb/s for %fkb" % (recv_rate, num_kbytes))
            best_recv_rate=recv_rate
        datum+=SEND_RATE_INCREMENT
        RECV_DONE.set() #tell the send thread to resume sending        
    sock.close()
   
        

if __name__ == '__main__':
    register_stanza_plugins()
    parser = argparse.ArgumentParser(description='hexchat commands')
    parser.add_argument('--debug', const="debug", nargs='?', default=False, help='run in debug mode')
    parser.add_argument('--logfile', dest='logfile', type=str, nargs=1, help='the log file')
    parser.add_argument('--server', dest='server', type=str, nargs=2)
    parser.add_argument('--client', dest='client', type=str, nargs=2)
    parser.add_argument('--num_logins', const='num_logins', type=int, nargs='?', default=1, help='number of times to login to each account')
    parser.add_argument('--sequential_bootup', const='sequential_bootup', nargs='?', default=False, help='Some computers need to login to each account sequentially. If hexchat never boots up, try setting this option.')
    args=parser.parse_args()

    if args.debug:
        logging.basicConfig(filename=args.logfile[0],level=logging.DEBUG)
    else:
        logging.basicConfig(filename=args.logfile[0],level=logging.WARN)
        
    server_socket=socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setblocking(1)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_socket.bind(('127.0.0.1',54321))
    server_socket.listen(1024)
    
    server=master([args.server], None, args.num_logins, args.sequential_bootup)    
    client=master([args.client], None, args.num_logins, args.sequential_bootup)
    client.create_server_socket(('127.0.0.1',12345), args.server[0], ('127.0.0.1',54321))
    
    client_socket=socket.create_connection(('127.0.0.1',12345))
    client_socket.setblocking(1)
    (server_socket, _)=server_socket.accept()
    r=threading.Thread(name="recv %d" % 1, target=lambda: recv(client_socket))
    s=threading.Thread(name="send %d" % 1, target=lambda: send(server_socket, server))
    s.start()
    r.start()
    while r.isAlive() and s.isAlive():
        time.sleep(1)
