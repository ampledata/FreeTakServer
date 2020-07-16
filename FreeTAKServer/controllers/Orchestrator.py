#######################################################
# 
# orchestrator.py
# Python implementation of the Class orchestrator
# Generated by Enterprise Architect
# Created on:      21-May-2020 12:24:48 PM
# Original author: Natha Paquette
# 
#######################################################
from importlib import import_module
import os
from FreeTAKServer.controllers.ReceiveConnections import ReceiveConnections
from FreeTAKServer.controllers.ClientInformationController import ClientInformationController
from FreeTAKServer.controllers.ClientSendHandler import ClientSendHandler
from FreeTAKServer.controllers.SendClientData import SendClientData
from FreeTAKServer.controllers.DataQueueController import DataQueueController
from FreeTAKServer.controllers.ClientInformationQueueController import ClientInformationQueueController
from FreeTAKServer.controllers.ActiveThreadsController import ActiveThreadsController
from FreeTAKServer.controllers.ReceiveConnectionsProcessController import ReceiveConnectionsProcessController
from FreeTAKServer.controllers.MainSocketController import MainSocketController
from FreeTAKServer.controllers.XMLCoTController import XMLCoTController
from FreeTAKServer.controllers.SendOtherController import SendOtherController
from FreeTAKServer.controllers.SendDataController import SendDataController
from FreeTAKServer.controllers.AsciiController import AsciiController
from FreeTAKServer.controllers.configuration.LoggingConstants import LoggingConstants
from FreeTAKServer.controllers.configuration.SQLcommands import SQLcommands as sql
from FreeTAKServer.controllers.configuration.DataPackageServerConstants import DataPackageServerConstants as DPConst
from FreeTAKServer.controllers.configuration.OrchestratorConstants import OrchestratorConstants
from FreeTAKServer.controllers.configuration.DataPackageServerConstants import DataPackageServerConstants
from FreeTAKServer.controllers.HealthCheckController import HealthCheckController
from FreeTAKServer.controllers.model.RawCoT import RawCoT

ascii = AsciiController().ascii
import sys
from logging.handlers import RotatingFileHandler
import logging
import FreeTAKServer.controllers.DataPackageServer as DataPackageServer
import multiprocessing
import threading
import time
import pickle
import importlib
import argparse
import sqlite3
import socket
import atexit
from signal import signal, SIGTERM

loggingConstants = LoggingConstants()

from FreeTAKServer.controllers.ClientReceptionHandler import ClientReceptionHandler


class Orchestrator:
    # default constructor  def __init__(self):
    def __init__(self):
        log_format = logging.Formatter(loggingConstants.LOGFORMAT)
        self.logger = logging.getLogger(loggingConstants.LOGNAME)
        self.logger.setLevel(logging.DEBUG)
        self.logger.addHandler(self.newHandler(loggingConstants.DEBUGLOG, logging.DEBUG, log_format))
        self.logger.addHandler(self.newHandler(loggingConstants.WARNINGLOG, logging.WARNING, log_format))
        self.logger.addHandler(self.newHandler(loggingConstants.INFOLOG, logging.INFO, log_format))
        # create necessary queues
        self.clientInformationQueue = []
        # this contains a list of all pipes which are transmitting CoT from clients
        self.pipeList = []
        # Internal Pipe used for CoT generated by the server itself
        self.internalCoTArray = []
        self.ClientReceptionHandlerEventPipe = ''
        # health check events
        self.healthCheckEventArray = []
        # instantiate controllers
        self.m_ActiveThreadsController = ActiveThreadsController()
        self.m_ClientInformationController = ClientInformationController()
        self.m_ClientInformationQueueController = ClientInformationQueueController()
        self.m_ClientSendHandler = ClientSendHandler()
        self.m_DataQueueController = DataQueueController()
        self.m_ReceiveConnections = ReceiveConnections()
        self.m_ReceiveConnectionsProcessController = ReceiveConnectionsProcessController()
        self.m_MainSocketController = MainSocketController()
        self.m_XMLCoTController = XMLCoTController()
        self.m_SendClientData = SendClientData()
        self.KillSwitch = 0
        self.openSockets = 0

    def newHandler(self, filename, log_level, log_format):
        handler = RotatingFileHandler(
            filename,
            maxBytes=loggingConstants.MAXFILESIZE,
            backupCount=loggingConstants.BACKUPCOUNT
        )
        handler.setFormatter(log_format)
        handler.setLevel(log_level)
        return handler

    def clientConnected(self, rawConnectionInformation):
        #TODO: remove client pipe and requirements
        try:
            self.openSockets += 1
            clientPipe = None
            self.logger.info(loggingConstants.CLIENTCONNECTED)
            clientInformation = self.m_ClientInformationController.intstantiateClientInformationModelFromConnection(
                rawConnectionInformation, clientPipe)
            if self.checkOutput(clientInformation):
                pass
            else:
                raise Exception('error in the creation of client information')
            self.m_ClientInformationQueueController.addClientToQueue(clientInformation)
            self.clientInformationQueue.append(clientInformation)
            try:
                self.db.commit()
                cursor = self.db.cursor()
                cursor.execute(sql().ADDUSER, (
                clientInformation.modelObject.uid, clientInformation.modelObject.m_detail.m_Contact.callsign))
                self.db.commit()
            except Exception as e:
                self.logger.error('there has been an error in a clients connection while adding information to the database ' + str(e))
            self.logger.info(loggingConstants.CLIENTCONNECTEDFINISHED + str(clientInformation.modelObject.m_detail.m_Contact.callsign))
            self.clientDataPipe.send(['add', clientInformation, self.openSockets])
            return clientInformation
        except Exception as e:
            self.logger.error(loggingConstants.CLIENTCONNECTEDERROR + str(e))
            return -1

    def emergencyReceived(self, processedCoT):
        try:
            if processedCoT.status == loggingConstants.ON:
                self.internalCoTArray.append(processedCoT)
                self.logger.debug(loggingConstants.EMERGENCYCREATED)
            elif processedCoT.status == loggingConstants.OFF:
                for CoT in self.internalCoTArray:
                    if CoT.type == loggingConstants.EMERGENCY and CoT.modelObject.uid == processedCoT.modelObject.uid:
                        self.internalCoTArray.remove(CoT)
                        self.logger.debug(loggingConstants.EMERGENCYREMOVED)
        except Exception as e:
            self.logger.error(loggingConstants.EMERGENCYRECEIVEDERROR + str(e))

    def dataReceived(self, RawCoT):
        # this will be executed in the event that the use case for the CoT isnt specified in the orchestrator
        try:
            # this will check if the CoT is applicable to any specific controllers
            RawCoT = self.m_XMLCoTController.determineCoTType(RawCoT)
            # the following calls whatever controller was specified by the above function
            module = importlib.import_module('FreeTAKServer.controllers.' + RawCoT.CoTType)
            CoTSerializer = getattr(module, RawCoT.CoTType)
            processedCoT = CoTSerializer(RawCoT).getObject()
            sender = processedCoT.clientInformation
            # this will send the processed object to a function which will send it to connected clients
            try:
                if processedCoT.type != 'ping':
                    self.logger.debug('data received from ' + str(
                        processedCoT.clientInformation.modelObject.m_detail.m_Contact.callsign) + 'type is ' + processedCoT.type)
                else:
                    pass
                if processedCoT.type == loggingConstants.EMERGENCY:
                    self.emergencyReceived(processedCoT)
            except Exception as e:
                return -1
            return processedCoT
        except Exception as e:
            self.logger.error(loggingConstants.DATARECEIVEDERROR + str(e))
            return -1

    def sendInternalCoT(self):
        try:
            if len(self.internalCoTArray)>0:
                for processedCoT in self.internalCoTArray:
                    SendDataController().sendDataInQueue(processedCoT.clientInformation, processedCoT, self.clientInformationQueue)
            else:
                pass
            return 1
        except Exception as e:
            self.logger.error(loggingConstants.MONITORRAWCOTERRORINTERNALSCANERROR + str(e))
            return -1
    def clientDisconnected(self, clientInformation):
        # print(self.clientInformationQueue[0])
        # print(clientInformation)
        self.openSockets -= 1
        if isinstance(clientInformation, RawCoT):
            clientInformation = clientInformation.clientInformation
        else:
            pass
        try:
            self.clientDataPipe.send(['remove', clientInformation, self.openSockets])
            try:
                clientInformation.socket.shutdown(socket.SHUT_RDWR)
            except Exception as e:
                self.logger.error('error shutting socket down in client disconnection')
                pass
            try:
                clientInformation.socket.close()
            except Exception as e:
                self.logger.error('error closing socket in client disconnection')
                pass

            self.logger.info(loggingConstants.CLIENTDISCONNECTSTART)
            for client in self.clientInformationQueue:
                if client.ID == clientInformation.ID:
                    self.clientInformationQueue.remove(client)
                else:
                    pass
            try:
                self.m_ActiveThreadsController.removeClientThread(clientInformation)
                self.db.commit()
                cursor = self.db.cursor()
                cursor.execute(sql().REMOVEUSER, (clientInformation.modelObject.uid,))
                cursor.close()
                self.db.commit()
            except Exception as e:
                self.logger.error('there has been an error in a clients disconnection while adding information to the database')
                pass
            self.logger.info(loggingConstants.CLIENTDISCONNECTEND + str(clientInformation.modelObject.m_detail.m_Contact.callsign))
            return 1
        except Exception as e:
            self.logger.error(loggingConstants.CLIENTCONNECTEDERROR + str(e))
            pass

    def monitorRawCoT(self,data):
        # this needs to be the most robust function as it is the keystone of the program
        from FreeTAKServer.controllers.model.RawCoT import RawCoT
        # this will attempt to define the type of CoT along with the designated controller
        try:
            CoT = XMLCoTController().determineCoTGeneral(data)
            function = getattr(self, CoT[0])
            output = function(CoT[1])
            return output
        except Exception as e:
            self.logger.error(loggingConstants.MONITORRAWCOTERRORB + str(e))
            return -1

    def checkOutput(self, output):
        if output != -1 and output != None:
            return True
        else:
            return False

    def loadAscii(self):
        ascii()

    def mainRunFunction(self, clientData, receiveConnection, sock, pool, event, clientDataPipe, ReceiveConnectionKillSwitch):
        while True:
            try:
                self.clientDataPipe = clientDataPipe
                if event.is_set():
                    try:
                        if ReceiveConnectionKillSwitch.is_set():
                            try:
                                receiveConnection.successful()
                            except:
                                pass
                            ReceiveConnectionKillSwitch.clear()
                            receiveConnection = pool.apply_async(ReceiveConnections().listen,
                                                                 (sock,))
                        else:
                            receiveConnectionOutput = receiveConnection.get(timeout=0.01)
                            receiveConnection = pool.apply_async(ReceiveConnections().listen, (sock,))
                            CoTOutput = self.handel_connection_data(receiveConnectionOutput)

                    except multiprocessing.TimeoutError:
                        pass
                    except Exception as e:
                        self.logger.error('exception in receive connection within main run function '+str(e))

                    try:
                        clientDataOutput = clientData.get(timeout=0.01)
                        clientData = pool.apply_async(ClientReceptionHandler().startup, (self.clientInformationQueue,))
                        if self.checkOutput(clientDataOutput) and isinstance(clientDataOutput, list):
                            CoTOutput = self.handel_regular_data(clientDataOutput)
                        else:
                            raise Exception('client reception handler has returned data which is not of type list data is ' + str(clientDataOutput))
                    except multiprocessing.TimeoutError:
                        pass
                    except Exception as e:
                        self.logger.info('exception in receive client data within main run function ' + str(e))
                        pass
                else:
                    self.stop()
                    break
            except Exception as e:
                self.logger.info('there has been an uncaught error thrown in mainRunFunction' + str(e))
                pass

    def handel_regular_data(self, clientDataOutput):
        try:
            for clientDataOutputSingle in clientDataOutput:
                try:
                    CoTOutput = self.monitorRawCoT(clientDataOutputSingle)
                    if CoTOutput == 1:
                        continue
                    elif self.checkOutput(CoTOutput):
                        output = SendDataController().sendDataInQueue(CoTOutput.clientInformation, CoTOutput,
                                                                      self.clientInformationQueue)
                        if self.checkOutput(output) and isinstance(output, tuple) == False:
                            pass
                        elif isinstance(output, tuple):
                            self.logger.error('issue sending data to client now disconnecting')
                            self.clientDisconnected(output[1])

                        else:
                            self.logger.error('send data failed in main run function with data ' + str(
                                CoTOutput.xmlString) + ' from client ' + CoTOutput.clientInformation.modelObject.m_detail.m_Contact.callsign)

                    else:
                        raise Exception('error in general data processing')
                except Exception as e:
                    self.logger.info(
                        'exception in client data, data processing within main run function ' + str(
                            e) + ' data is ' + str(CoTOutput))
                    return -1
                except Exception as e:
                    self.logger.info(
                        'exception in client data, data processing within main run function ' + str(
                            e) + ' data is ' + str(clientDataOutput))
        except Exception as e:
            self.logger.info("there has been an error iterating client data output " + str(e))
            return -1
        self.sendInternalCoT()
        return 1

    def handel_connection_data(self, receiveConnectionOutput):
        try:
            CoTOutput = self.monitorRawCoT(receiveConnectionOutput)
            if CoTOutput != -1 and CoTOutput != None:
                output = SendDataController().sendDataInQueue(CoTOutput, CoTOutput,
                                                              self.clientInformationQueue)
                if self.checkOutput(output):
                    self.logger.debug('connection data from client ' + str(
                        CoTOutput.modelObject.m_detail.m_Contact.callsign) + ' successfully processed')
                else:
                    raise Exception('error in sending data')
            else:
                raise Exception('error in connection data processing')
        except Exception as e:
            self.logger.error('exception in receive connection data processing within main run function ' + str(
                e) + ' data is ' + str(CoTOutput))
            return -1
        return 1

    def start(self, IP, CoTPort, Event, clientDataPipe, ReceiveConnectionKillSwitch):
        try:
            self.db = sqlite3.connect(DPConst().DATABASE)
            os.chdir('../../')
            # create socket controller
            self.m_MainSocketController.changeIP(IP)
            self.m_MainSocketController.changePort(CoTPort)
            sock = self.m_MainSocketController.createSocket()
            pool = multiprocessing.Pool(processes=2)
            self.pool = pool
            clientData = pool.apply_async(ClientReceptionHandler().startup, (self.clientInformationQueue,))
            receiveConnection = pool.apply_async(ReceiveConnections().listen, (sock,))
            # instantiate domain model and save process as object
            self.logger.info('server has started')
            self.mainRunFunction(clientData, receiveConnection, sock, pool, Event, clientDataPipe, ReceiveConnectionKillSwitch)

        except Exception as e:
            self.logger.critical('there has been a critical error in the startup of FTS' + str(e))
            return -1

    def stop(self):
        self.clientDataPipe.close()
        self.pool.terminate()
        self.pool.close()
        self.pool.join()



if __name__ == "__main__":

    parser = argparse.ArgumentParser(description=OrchestratorConstants().FULLDESC)
    parser.add_argument(OrchestratorConstants().COTPORTARG, type=int, help=OrchestratorConstants().COTPORTDESC,
                        default=OrchestratorConstants().COTPORT)
    parser.add_argument(OrchestratorConstants().IPARG, type=str, help=OrchestratorConstants().IPDESC,
                        default=OrchestratorConstants().IP)
    parser.add_argument(OrchestratorConstants().APIPORTARG, type=int, help=OrchestratorConstants().APIPORTDESC,
                        default=DataPackageServerConstants().APIPORT)
    args = parser.parse_args()
    CreateStartupFilesController()
    Orchestrator().start(args.IP, args.CoTPort, args.APIPort)