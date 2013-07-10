import argparse
import socket
import threading
import time
import logging

from master import master
from stanza_plugins import register_stanza_plugins
import sys

if sys.version_info < (3, 0):
    reload(sys)
    sys.setdefaultencoding('utf8')
else:
    raw_input = input

START_SEND_RATE=1000
END_SEND_RATE=100000
SEND_RATE_INCREMENT=1000

def send(sock):
    for datum in range(START_SEND_RATE, END_SEND_RATE, SEND_RATE_INCREMENT):
        str_datum=str(datum)
        str_datum='0'*(8-len(str_datum))+str_datum
        data=str_datum.encode("UTF-8")*datum
        then=time.time()
        time.sleep(1)
        while data:
            bytes=sock.send(data)
            data=data[bytes:]
        send_rate=8*datum/(time.time()-then)
        logging.warn("sent %d bytes at %fkb/s" % (8*datum, send_rate))
    sock.close()

def recv(sock):
    datum=START_SEND_RATE
    counter=0
    best_recv_rate=0
    then=time.time()
    while datum<END_SEND_RATE:
        data=sock.recv(2**17)
        while data:
            received_datum=int(data[:8])
            assert received_datum==datum
            counter+=1
            if counter==datum:
                recv_rate=8*datum/(time.time()-then)
                logging.warn("received %d bytes at %fkb/s" % (8*datum, recv_rate))
                if recv_rate>best_recv_rate:
                    logging.warn("best recv rate is %fkb/s for %d bytes" % (recv_rate, 8*datum))
                    best_recv_rate=recv_rate
                counter=0
                datum+=SEND_RATE_INCREMENT
                then=time.time()
            data=data[8:]
    sock.close()
   
        

if __name__ == '__main__':
    register_stanza_plugins()
    parser = argparse.ArgumentParser(description='hexchat commands')
    parser.add_argument('--logfile', dest='logfile', type=str, nargs=1, help='the log file')
    parser.add_argument('--server', dest='server', type=str, nargs=2)
    parser.add_argument('--client', dest='client', type=str, nargs=2)
    parser.add_argument('--num_logins', const='num_logins', type=int, nargs='?', default=1, help='number of times to login to each account')
    parser.add_argument('--sequential_bootup', const='sequential_bootup', nargs='?', default=False, help='Some computers need to login to each account sequentially. If hexchat never boots up, try setting this option.')
    args=parser.parse_args()
    
    logging.basicConfig(filename=args.logfile[0],level=logging.WARN)
    
    server_socket=socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setblocking(1)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_socket.bind(('127.0.0.1',54321))
    server_socket.listen(1024)
    
    client=master([args.client], None, args.num_logins, args.sequential_bootup)
    server=master([args.server], None, args.num_logins, args.sequential_bootup)
    client.create_server_socket(('127.0.0.1',12345), args.server[0], ('127.0.0.1',54321))
    
    client_socket=socket.create_connection(('127.0.0.1',12345))
    client_socket.setblocking(1)
    (server_socket, _)=server_socket.accept()
    r=threading.Thread(name="recv %d" % 1, target=lambda: recv(client_socket))
    s=threading.Thread(name="send %d" % 1, target=lambda: send(server_socket))
    r.start()
    s.start()
    while r.isAlive() and s.isAlive():
        time.sleep(1)
