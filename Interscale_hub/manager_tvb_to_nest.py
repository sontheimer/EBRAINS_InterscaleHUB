# ------------------------------------------------------------------------------
#  Copyright 2020 Forschungszentrum Jülich GmbH
# "Licensed to the Apache Software Foundation (ASF) under one or more contributor
#  license agreements; and to You under the Apache License, Version 2.0. "
#
# Forschungszentrum Jülich
#  Institute: Institute for Advanced Simulation (IAS)
#    Section: Jülich Supercomputing Centre (JSC)
#   Division: High Performance Computing in Neuroscience
# Laboratory: Simulation Laboratory Neuroscience
#       Team: Multi-scale Simulation and Design
# ------------------------------------------------------------------------------

import numpy as np
import os
import time

from EBRAINS_InterscaleHUB.Interscale_hub.communicator_tvb_to_nest import CommunicatorTvbNest                      
from EBRAINS_InterscaleHUB.Interscale_hub.manager_base import InterscaleHubBaseManager                                   
from EBRAINS_InterscaleHUB.Interscale_hub.interscalehub_enums import DATA_EXCHANGE_DIRECTION, DATA_BUFFER_TYPES, DATA_BUFFER_STATES
from EBRAINS_RichEndpoint.application_companion.common_enums import Response
from EBRAINS_RichEndpoint.application_companion.common_enums import INTERCOMM_TYPE 
from EBRAINS_ConfigManager.global_configurations_manager.xml_parsers.default_directories_enum import DefaultDirectories 


class TvbToNestManager(InterscaleHubBaseManager):
    """
    Implements the InterscaleHubBaseManager to
    1) Interact with InterscaleHub Facade to steer the execution
    2) Manage the InterscaleHub functionality.
    """
    # NOTE two different sources of parameters
    # TODO Refactoring
    def __init__(self, parameters, configurations_manager, log_settings,
                 direction,
                 sci_params_xml_path_filename=''):
        """
            Init params, create buffer, open ports, accept connections

        :param parameters:
        :param direction: <-- # TO BE DONE: Check whether it could be removed since it is not being used
        :param configurations_manager:
        :param log_settings:
        :param sci_params_xml_path_filename:
        """

        self.__log_settings = log_settings
        self.__configurations_manager = configurations_manager
        self.__logger = self.__configurations_manager.load_log_configurations(
                                        name="InterscaleHub -- TVB_TO_NEST Manager",
                                        log_configurations=self.__log_settings,
                                        target_directory=DefaultDirectories.SIMULATION_RESULTS)
        
        self.__logger.debug(f"host_name:{os.uname()}")
        # 1) param stuff, create IntercommManager
        self.__logger.debug("Init Params...")
        super().__init__(parameters,
                         #  DATA_EXCHANGE_DIRECTION.TVB_TO_NEST,
                         direction,
                         self.__configurations_manager,
                         self.__log_settings,
                         sci_params_xml_path_filename=sci_params_xml_path_filename)
        self.__tvb_nest_communicator = None
        # done: TODO: set via XML settings? POD
        # self.__buffersize = 2 + self._max_events  # 2 doubles: [start_time,end_time] of simulation step
        
        # NOTE input and output buffers are of the same size
        self.__buffersize = self._sci_params.max_events + self._sci_params.tvb_buffer_size_factor
        
        # TODO set it via XML configurations file
        # NOTE Refactoring of communication protocols and data aggregation and
        # management is needed when more than one ranks are used for
        # intercommunication with simulators
        self.__group_of_ranks_for_receiving  = [1]  # NOTE hardcoded
        self.__group_of_ranks_for_sending  = [0]  # NOTE hardcoded

        # NOTE all remaining ranks are transformers
        self.__group_of_ranks_for_transformation = [x for x in range(self._intra_comm.Get_size())
                                                    if x not in (self.__group_of_ranks_for_receiving + self.__group_of_ranks_for_sending) ]

        # 2) MPI groups setup
        self.__logger.info("setting up mpi groups...")
        self.__setup_mpi_groups_and_comms()
        self.__logger.info("mpi groups are set up.")

        # 3) create buffers
        self.__logger.info("Creating MPI shared memory Buffer...")
        #  set up shared memory input buffer
        self._databuffer_input = self._get_mpi_shared_memory_buffer(self.__buffersize, self._intra_comm, DATA_BUFFER_TYPES.INPUT)
        self.__logger.info("input Buffer created")
        if self._mpi_com_group_receivers and self._intra_comm.Get_rank() == self.__group_of_ranks_for_receiving[0]:
            self.__set_initial_buffer_state(state=DATA_BUFFER_STATES.READY_TO_RECEIVE, buffer_type=DATA_BUFFER_TYPES.INPUT)

        # sync up point so that initial state of the INPUT buffer could be set
        self.__logger.debug("sync up point to set up the initial sta")
        self._intra_comm.Barrier()
        # check if bugger state is the same for all ranks
        input_buffer_state = self._interscalehub_buffer_manager.get_at(index=-1,
                                                    buffer_type=DATA_BUFFER_TYPES.INPUT)
        self.__logger.debug(f"manager input_buffer_state: {input_buffer_state}")
        
        # 4) Data channel setup
        self.__input_comm = None
        self.__output_comm = None
        self.__logger.info("setting up data channels...")
        self.__data_channel_setup()
        self.__logger.info("data channels open and ready.")
        
        self.__logger.info("initialized")
    

    def __set_initial_buffer_state(self, state, buffer_type):
        self._interscalehub_buffer_manager.set_ready_state_at(index=-1,
                                                     state=state,
                                                     buffer_type=buffer_type)
        self.__logger.debug(f"set up initial state: {state.name} of the buffer: {buffer_type.name}")

    def __data_channel_setup(self):
        """
        Open ports and register connection details.
        Accept connection on ports and create INTER communicators.

        MVP: register = write port details to file.
        MVP: Two connections
            - input = incoming simulation data
            - output = outgoing simulation data
        """
        # NOTE: create port files and make connection
        # In Demo example: producer/Consumer are inherited from mpi_io_extern,
        # and then they are started as threads which then call mpi_io_extern run() method
        # which then calls make_connection() method

        # Case: data exchange direction is from NEST-to-TVB
        if self._intra_comm.Get_rank() in self.__group_of_ranks_for_receiving:  
            self.__input_comm, self.__input_port = self._set_up_connection(
                direction=DATA_EXCHANGE_DIRECTION.TVB_TO_NEST.name,
                intercomm_type=INTERCOMM_TYPE.RECEIVER.name)
            self.__output_comm = None

        elif self._intra_comm.Get_rank() in self.__group_of_ranks_for_sending:
        # elif self._intra_comm.Get_rank() == 0:
            self.__output_comm, self.__output_port = self._set_up_connection(
                direction=DATA_EXCHANGE_DIRECTION.TVB_TO_NEST.name,
                intercomm_type=INTERCOMM_TYPE.SENDER.name)
            self.__input_comm = None

    def __setup_mpi_groups_and_comms(self):
        """
            helper function to group mpi processes based on their functionality
        """
        if self._intra_comm.Get_rank() == self.__group_of_ranks_for_receiving[0]:  
            self._mpi_com_group_receivers = self._setup_mpi_groups_including_ranks(self.__group_of_ranks_for_receiving)

        elif self._intra_comm.Get_rank() == self.__group_of_ranks_for_sending[0]:
            self._mpi_com_group_senders = self._setup_mpi_groups_including_ranks(self.__group_of_ranks_for_sending)

        elif self._intra_comm.Get_rank() in self.__group_of_ranks_for_transformation:
            self._mpi_com_group_transformers = self._setup_mpi_groups_including_ranks(self.__group_of_ranks_for_transformation)

    def start(self, id_first_spike_detector):
        """
        implementation of abstract method to start transformation and
        exchanging the data with TVB and NEST.
        """
        self.__logger.info("Start data transfer and use case science...")
        
        # initialize Communicator
        self.__tvb_nest_communicator = CommunicatorTvbNest(
            self.__configurations_manager,
            self.__log_settings,
            self._interscalehub_buffer_manager,
            self._mediator)
        
        # start exchanging the data
        if self.__tvb_nest_communicator.start(self._intra_comm,
                                              self.__input_comm,
                                              self.__output_comm,
                                              id_first_spike_detector,
                                              self._mpi_com_group_senders,
                                              self._mpi_com_group_receivers,
                                              self._mpi_com_group_transformers,
                                              self.__group_of_ranks_for_sending,
                                              self.__group_of_ranks_for_receiving,
                                              self.__group_of_ranks_for_transformation) == Response.ERROR:
            # Case a: something went wrong during the data exchange
            # NOTE the details are already been logged at the origin of the error
            # now terminate with error
            self.__logger.critical('Got error while exchanging the data.')

            # sync up point
            self.__logger.debug("sync up point before terminating")
            self._intra_comm.Barrier()
            return Response.ERROR
        else:
            # Case b: everything went well
            # sync up point
            self.__logger.debug("sync up point before terminating")
            self._intra_comm.Barrier()
            return Response.OK

    def stop(self):
        """
        implementation of the abstract method to conclude the pivot operations
        and stop exchanging the data.
        TODO: add error handling and fail checks
        """
        self.__logger.info("Stop InterscaleHub and disconnect...")
        self.__tvb_nest_communicator.stop()
        # if self._intra_comm.Get_rank() == 0:
        if self._intra_comm.Get_rank() in self.__group_of_ranks_for_sending:
            self._intercomm_manager.close_and_finalize(self.__output_comm, self.__output_port)
        elif self._intra_comm.Get_rank() in self.__group_of_ranks_for_receiving:
            self._intercomm_manager.close_and_finalize(self.__input_comm, self.__input_port)
