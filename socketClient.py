# -*- coding: utf-8 -*-
"""
Created on Fri Oct 16 11:17:29 2015

@author: dlerlp
"""

import zmq

# How to connect to the TCP servers started by imageViewer2
context = zmq.Context()

# Subscribe to beam size and position updates
subSocket = context.socket(zmq.SUB)
subSocket.setsockopt(zmq.CONFLATE, 1) #keep only the last update
subSocket.connect('tcp://localhost:5556')
subSocket.setsockopt_string(zmq.SUBSCRIBE, '') #subscribe to all updates
data = subSocket.recv_pyobj()
print(data)

# Control the selected screen by sending requests
clientSocket = context.socket(zmq.REQ)
clientSocket.connect('tcp://localhost:5559')
clientSocket.send_string('INJ-2')
msg = clientSocket.recv_string()
print(msg) #should be 'INJ-2'
