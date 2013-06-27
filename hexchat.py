#!/usr/bin/env python
import logging
import sys
import argparse
import time
from master import master
from stanza_plugins import register_stanza_plugins

# Python versions before 3.0 do not use UTF-8 encoding
# by default. To ensure that Unicode is handled properly
# throughout SleekXMPP, we will set the default encoding
# ourselves to UTF-8.
if sys.version_info < (3, 0):
    reload(sys)
    sys.setdefaultencoding('utf8')
else:
    raw_input = input

if __name__ == '__main__':
    register_stanza_plugins()
    parser = argparse.ArgumentParser(description='hexchat commands')
    parser.add_argument('--logfile', dest='logfile', type=str, nargs=1, help='the log file')
    parser.add_argument('--debug', const="debug", nargs='?', default=False, help='run in debug mode')
    parser.add_argument('--login', dest='login', type=str, nargs='+', help="login1 'password1 login2 'pasword2' ...")
    parser.add_argument('--whitelist', dest='whitelist', nargs='*', default=None, help='whitelist of ips and ports this computer can connect to. ip1 port1 ip2 port2 ...')  
    parser.add_argument('--client', dest='client', type=str, nargs='+', default=False, help='Ports to listen on and JIDs and ports to forward to. <local ip1> <local port1> <server jid1> <remote ip1> <remote port1> ...')
    args=parser.parse_args()

    if args.debug:
        logging.basicConfig(filename=args.logfile[0],level=logging.DEBUG)
    else:
        logging.basicConfig(filename=args.logfile[0],level=logging.WARN)
            
    username_passwords=[]
    index=0
    while index<len(args.login):
        username_passwords.append((args.login[index], args.login[index+1]))
        index+=2

    if args.whitelist==None:
        whitelist=None
    else:
        whitelist=[]
        index=0
        while index<len(args.whitelist):
            whitelist.append((args.whitelist[index], int(args.whitelist[index+1])))
            index+=2       

    master0=master(username_passwords, whitelist)
    if args.client:
        index=0
        while index<len(args.client):
            master0.create_server_socket((args.client[index],int(args.client[index+1])), args.client[index+2], (args.client[index+3],int(args.client[index+4])))
            index+=5
            
    while True:
        time.sleep(1)
