#!/usr/bin/env python3

import time
import sys
import socket
import logging
import asyncio
import signal
import functools
import yaml
import json

from typing import Dict
import paho.mqtt.client as mqtt

#from __future__ import division
 
# Import the PCA9685 module.
import Adafruit_PCA9685
 

class MqttSpy:

    # basic functionality to set a motor to a given position
    def set_motor_angle( self, motor, angle ):

        # min and max servo pulse lengths
        servo_min = 150+50  # Min pulse length out of 4096
        servo_max = 600+50  # Max pulse length out of 4096

        if angle < 0 : angle= 0
        if angle > 180 : angle= 180

        self.pwm.set_pwm( motor, 0, servo_min+angle*(servo_max-servo_min)//180 )

    def motor_off( self, motor ):
        self.pwm.set_pwm(motor, 0, 0)

    # full service: set motor to given position, wait minimal time, turn motor off again
    def set_motor( self, motor, angle ):

        self.set_motor_angle( motor, int( angle ) )
        time.sleep(0.2)
        self.motor_off( motor )

    def mqtt_callback_connect(self, client, userdata, flags, rc):

        #(result, _) = self.mqtt_client.subscribe( self.mqtt_topic )
        #self.logger.info( "MQTT subscription result for {}: {}".format(self.mqtt_topic,result) )
        for i in self.conf['devices']:
            (result, _) = self.mqtt_client.subscribe( self.conf['devices'][i]['topic'] )
            self.logger.info( "MQTT subscription result for {}: {}".format(self.conf['devices'][i]['topic'],result) )


    def mqtt_callback_message( self, client, userdata, msg ):

        print( "MQTT message ", msg.topic, " : ", msg.payload, end='' )
        if msg.topic in self.state:
            s= self.state[msg.topic]
            #print( "    Have device for ", msg.topic )
            payload = msg.payload.decode("utf-8")
            #print( "    Payload ", payload )
            obj= json.loads(payload)
            #print( "    Object ", obj )
            if len(obj.keys()) != 1:
                print( "        Error: multiple entries in ", obj )
            v= 0.0
            for o in obj:
                #print( "        key ", o, " value " , obj[o] )
                v= float( obj[o] )
                
            #print( "    Value ", v )

            if v < s['min_value']: v= s['min_value']
            if v > s['max_value']: v= s['max_value']
            #print( "    Value ", v )

            d= s['start'] + ( v - s['min_value'] ) * s['factor']
            #print( "    Degree ", d )

            delta= 1 # only perform change if it changes delta or more degrees
            if abs( d - s['last'] ) >= delta:
                print( "    Value ", v, " degree ", d, " for motor ", s['motor'] )

                # change to new position
                self.set_motor( s['motor'], d )
                s['last']= d
            else:
                # skip change because it would be too small
                print( "    Value ", v, " degree ", d, " for motor ", s['motor'], " -- already there" )


    def mqtt_callback_disconnect(self, client, userdata, rc):
        """ mqtt callback when the client gets disconnected """
        if rc != 0:
            self.logger.warning( "MQTT: unexpected disconnection." )


    def signal_handler( self, signame, loop ):
        """ catches signals and finishes the main loop

        Args:
            signame: the incoming signal
            loop: reference to the loop
        """
        self.logger.info( "got signal %s: exit", signame )
        loop.stop()


    def __init__( self, configfile: str, log_level= logging.WARNING ):

        self.logger= logging.getLogger("mqtt_spy")
        self.logger.setLevel(log_level)

        self.logger.info( "MqttSpy __init__ start" )

        with open( configfile, 'r' ) as stream:
            try:
                self.conf = yaml.load( stream, Loader=yaml.SafeLoader )
            except yaml.YAMLError as exc:
                self.logger.critical( exc )
                self.logger.critical( "Unable to parse configuration file {}".format(configfile) )
                sys.exit(1)

        self.pwm = Adafruit_PCA9685.PCA9685()
        self.pwm.set_pwm_freq(60)

        self.state= dict()

        print( "Config: " )
        print( self.conf ) 
        print( " ---- " )
        for i in self.conf['devices']:
            print( "    ", i, " --> ", self.conf['devices'][i]['topic'] )
            min_value= float( self.conf['devices'][i]['min_value'] )
            max_value= float( self.conf['devices'][i]['max_value'] )
            min_degree= float( self.conf['devices'][i]['min_degree'] )
            max_degree= float( self.conf['devices'][i]['max_degree'] )
            
            # turn min/max value degree into a function f: [min_value, maxvalue] --> [min_degree,max_degree]
            # with d= f(v)= start + ( v - min_value ) * factor, determine 'start' and 'factor' here
            # this works for min_degree < max_degree and min_degree > max_degree
            
            if min_value >= max_value :
                self.logger.critical( "Critical error in device %s: min_value >= max_value is not allowed", i )
                sys.exit(1)
                
            key= self.conf['devices'][i]['topic']
            value= dict()
            value['start']= min_degree
            value['factor']= ( max_degree - min_degree ) / ( max_value - min_value )
            value['motor']= int( self.conf['devices'][i]['motor'] )
            value['min_value']= min_value
            value['max_value']= max_value
            value['min_degree']= min_degree
            value['max_degree']= max_degree
            value['last']= 0

            self.state[key]= value ## add to configured devices

            # set motor to 0 position
            self.set_motor( value['motor'], value['last'] )

        self.update_intervall= 60

        self.mqtt_client= mqtt.Client( client_id="mqtt spy on "+str(socket.gethostname()) )
        self.mqtt_client.on_connect = self.mqtt_callback_connect
        self.mqtt_client.on_message = self.mqtt_callback_message
        self.mqtt_client.on_disconnect = self.mqtt_callback_disconnect

        self.mqtt_client.username_pw_set( username= self.conf['mqtt']['user'], password= self.conf['mqtt']['password'] )
        self.mqtt_client.connect( self.conf['mqtt']['server'], self.conf['mqtt']['port'], keepalive= 60 )
        self.mqtt_client.loop_start()

        self.logger.info( "MqttSpy __init__ start")

    def loop_forever(self):

        self.logger.info( "starting main loop with %d seconds intervall", int(self.update_intervall) )
        self.loop = asyncio.get_event_loop()

        # install signal handlers for graceful exit
        for signame in {'SIGINT', 'SIGTERM'}:
            self.loop.add_signal_handler( getattr(signal, signame), 
                functools.partial( MqttSpy.signal_handler, self, signame, self.loop ) )

        # schedule regular update for the first time
        #self.loop.call_soon(IoTRuntime.regular_update, self, self.loop)

        try:
            self.loop.run_forever()
        except:
            self.logger.error("unexpected error via exception")
        finally:
            self.loop.close()


if __name__ == '__main__':

    obj = MqttSpy( "setup.yaml", logging.WARNING )
    obj.loop_forever()
