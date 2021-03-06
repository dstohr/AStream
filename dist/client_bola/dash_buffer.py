from __future__ import division
import Queue
import threading
import time
import csv
import os
import config_dash, dash_client
from stop_watch import StopWatch
import dash_event_logger
# Durations in seconds
PLAYER_STATES = ['INITIALIZED', 'INITIAL_BUFFERING', 'PLAY',
                 'PAUSE', 'BUFFERING', 'STOP', 'END']
EXIT_STATES = ['STOP', 'END']


class DashPlayer:
    """ DASH buffer class """
    def __init__(self, video_length, segment_duration):
        config_dash.LOG.info("Initializing the Buffer")
        self.player_thread = None
        self.playback_start_time = None
        self.playback_duration = video_length
        print self.playback_duration
        self.segment_duration = segment_duration
        # Timers to keep track of playback time and the actual time
        self.playback_timer = StopWatch()
        self.actual_start_time = None
        # Playback State
        self.playback_state = "INITIALIZED"
        self.playback_state_lock = threading.Lock()
        # Buffer size
        if config_dash.MAX_BUFFER_SIZE:
            self.max_buffer_size = config_dash.MAX_BUFFER_SIZE
        else:
            self.max_buffer_size = video_length
        # Duration of the current buffer
        self.buffer_length = 0
        self.buffer_length_lock = threading.Lock()
        # Buffer Constants
        self.initial_buffer = config_dash.INITIAL_BUFFERING_COUNT
        self.alpha = config_dash.ALPHA_BUFFER_COUNT
        self.beta = config_dash.BETA_BUFFER_COUNT
        self.segment_limit = None
        # Current video buffer that holds the segment data
        # self.buffer = Queue.Queue()
        self.buffer = []
        self.buffer_lock = threading.Lock()
        self.current_segment = None
        self.buffer_log_file = config_dash.BUFFER_LOG_FILENAME
        config_dash.LOG.info("VideoLength={},segmentDuration={},MaxBufferSize={},InitialBuffer(secs)={},"
                             "BufferAlph(secs)={},BufferBeta(secs)={}".format(self.playback_duration,
                                                                              self.segment_duration,
                                                                              self.max_buffer_size, self.initial_buffer,
                                                                              self.alpha, self.beta))

    def set_state(self, state):
        """ Function to set the state of the player"""
        state = state.upper()
        if state in PLAYER_STATES:
            self.playback_state_lock.acquire()
            config_dash.LOG.info("Changing state from {} to {} at {} Playback time ".format(self.playback_state, state,
                                                                                            self.playback_timer.time()))
            self.playback_state = state
            self.playback_state_lock.release()
        else:
            config_dash.LOG.error("Unidentified state: {}".format(state))

    def initialize_player(self):
        """Method that update the current playback time"""
        start_time = time.time()
        initial_wait = 0
        paused = False
        buffering = False
        interruption_start = None
        config_dash.LOG.info("Initialized player with video length {}".format(self.playback_duration))
        while True:
            # Video stopped by the user
            if self.playback_state == "END":
                config_dash.LOG.info("Finished playback of the video: {} seconds of video played for {} seconds".format(
                    self.playback_duration, time.time() - start_time))
                self.playback_timer.pause()
                return "STOPPED"

            if self.playback_state == "STOP":
                # If video is stopped quit updating the playback time and exit player
                config_dash.LOG.info("Player Stopped at time {}".format(
                    time.time() - start_time))
                self.playback_timer.pause()
                self.log_entry("Stopped")
                return "STOPPED"

            # If paused by user
            if self.playback_state == "PAUSE":
                if not paused:
                    # do not update the playback time. Wait for the state to change
                    config_dash.LOG.info("Player Paused after {:4.2f} seconds of playback".format(
                        self.playback_timer.time()))
                    self.playback_timer.pause()
                    paused = True
                continue

            # If the playback encounters buffering during the playback
            if self.playback_state == "BUFFERING":
                if not buffering:
                    #dash_event_logger.bufferingStart("0000", time)
                    config_dash.LOG.info("Entering buffering stage after {} seconds of playback".format(
                        self.playback_timer.time()))
                    self.playback_timer.pause()
                    buffering = True
                    interruption_start = time.time()
                    config_dash.JSON_HANDLE['playback_info']['interruptions']['count'] += 1
                # If the size of the buffer is greater than the RE_BUFFERING_DURATION then start playback
                else:
                    # If the RE_BUFFERING_DURATION is greate than the remiang length of the video then do not wait
                    remaining_playback_time = self.playback_duration - self.playback_timer.time()
                    # if ((self.buffer.qsize() >= config_dash.RE_BUFFERING_COUNT) or (
                    if ((self.buffer.__len__() >= config_dash.RE_BUFFERING_COUNT) or ( #MZ
                            config_dash.RE_BUFFERING_COUNT * self.buffer_length >= remaining_playback_time
                            # and self.buffer.qsize() > 0)):
                            and self.buffer.__len__() > 0)): #MZ
                        buffering = False
                        if interruption_start:
                            interruption_end = time.time()
                            interruption = interruption_end - interruption_start
                            interruption_start = None
                            config_dash.JSON_HANDLE['playback_info']['interruptions']['events'].append(
                                (interruption_start, interruption_end))
                            config_dash.JSON_HANDLE['playback_info']['interruptions']['total_duration'] += interruption
                            config_dash.LOG.info("Duration of interruption = {}".format(interruption))
                        self.set_state("PLAY")
                        self.log_entry("Buffering-Play")
                        dash_event_logger.onStalling(interruption, self.playback_timer.time())

            if self.playback_state == "INITIAL_BUFFERING":
                # if self.buffer.qsize() < config_dash.INITIAL_BUFFERING_COUNT:
                if self.buffer.__len__() < config_dash.INITIAL_BUFFERING_COUNT: #MZ
                    initial_wait = time.time() - start_time
                    continue
                else:
                    config_dash.LOG.info("Initial Waiting Time = {}".format(initial_wait))
                    self.set_state("PLAY")
                    self.log_entry("InitialBuffering-Play")
                    res_event = {
                    'timestamp': "Thu, 23 Mar 2017 14:21:02", #FIXME getUTCTimestamp stops player
                    'eventtype': 'stalling',
                    'playback_position': 0,
                    'experiment': 0,
                    'eventtype': 'initialStalling',
                    'duration': initial_wait,
                    }
                    dash_event_logger.onInitStalling(res_event)

            if self.playback_state == "PLAY":
                    # Check of the buffer has any segments
                    if self.playback_timer.time() == self.playback_duration:
                        self.set_state("END")
                        self.log_entry("Play-End")
                    # if self.buffer.qsize() == 0:
                    if self.buffer.__len__() == 0: #MZ
                        config_dash.LOG.info("Buffer empty after {} seconds of playback".format(
                            self.playback_timer.time()))
                        self.playback_timer.pause()
                        self.set_state("BUFFERING")
                        self.log_entry("Play-Buffering")
                        continue
                    # Read one the segment from the buffer
                    # Acquire Lock on the buffer and read a segment for it
                    self.buffer_lock.acquire()
                    # play_segment = self.buffer.get()
                    play_segment = self.buffer.pop(0) #MZ
                    self.buffer_lock.release()
                    config_dash.LOG.info("Reading the segment number {} from the buffer at playtime {}".format(
                        play_segment['segment_number'], self.playback_timer.time()))
                    self.log_entry(action="StillPlaying", bitrate=play_segment["bitrate"])

                    # Calculate time playback when the segment finishes
                    future = self.playback_timer.time() + play_segment['playback_length']

                    # Start the playback
                    self.playback_timer.start()
                    while self.playback_timer.time() < future:
                        # If playback hasn't started yet, set the playback_start_time
                        if not self.playback_start_time:
                            self.playback_start_time = time.time()
                            config_dash.LOG.info("Started playing with representation {} at {}".format(
                                play_segment['bitrate'], self.playback_timer.time()))

                        # Duration for which the video was played in seconds (integer)
                        if self.playback_timer.time() >= self.playback_duration:
                            config_dash.LOG.info("Completed the video playback: {} seconds".format(
                                self.playback_duration))
                            self.playback_timer.pause()
                            self.set_state("END")
                            self.log_entry("TheEnd")
                            return
                    else:
                        self.buffer_length_lock.acquire()
                        self.buffer_length -= int(play_segment['playback_length'])
                        config_dash.LOG.debug("Decrementing buffer_length by {}. dash_buffer = {}".format(
                            play_segment['playback_length'], self.buffer_length))
                        self.buffer_length_lock.release()
                    if self.segment_limit:
                        if int(play_segment['segment_number']) >= self.segment_limit:
                            self.set_state("STOP")
                            config_dash.LOG.info("Stopped playback after segment {} at playtime {}".format(
                                play_segment['segment_number'], self.playback_duration))

    def write(self, segment):
        """ write segment to the buffer.
            Segment is dict with keys ['data', 'bitrate', 'playback_length', 'URI', 'size', 'segment_layer']
        """
        # Acquire Lock on the buffer and add a segment to it
        if not self.actual_start_time:
            self.actual_start_time = time.time()
        config_dash.LOG.info("Writing segment {} at time {}".format(segment['segment_number'],
                                                                    time.time() - self.actual_start_time))
        print "&$^@*#^$@"
        #print segment
        print segment['segment_number']
        print "^&(%^$&#"
        # MZ: Standard case. New segment arrives and is appended to the queue.
        if (not self.current_segment) or (self.current_segment < segment['segment_number']):
            print "------========"
            print "hello! Standard"
            self.buffer_lock.acquire()
            # self.buffer.put(segment)
            self.buffer.append(segment) #MZ
            self.buffer_lock.release()
            self.buffer_length_lock.acquire()
            self.buffer_length += int(segment['playback_length'])
            config_dash.LOG.debug("Incrementing buffer_length by {}. dash_buffer = {}".format(
                segment['playback_length'], self.buffer_length))
            self.buffer_length_lock.release()
            self.current_segment = segment['segment_number']

        # MZ: Retransmission case. Segment in better quality is retransmitted and replaces
        # existing segment.
        else:
            print "------========"
            print "hello! Replacing"
            self.buffer_lock.acquire()
            print self.buffer
            segment_numbers = [d['segment_number'] for d in self.buffer if 'segment_number' in d]
            for i in reversed(range(len(self.buffer))):
                if self.buffer[i].get('segment_number') == segment['segment_number']:
                    self.buffer.pop(i)
            segment_index = segment_numbers.index(segment['segment_number'])
            print "+++++++++++++"
            print self.buffer
            self.buffer.insert(segment_index, segment)
            #segment_numbers_new = [d['segment_number'] for d in self.buffer if 'segment_number' in d]
            print "^$^#%@#"
            print self.buffer
            #self.buffer.pop(100)
            #self.buffer.pop(segment.index(segment['segment_number']))
            #self.buffer.insert(segment, segment['segment_number'])
            self.buffer_lock.release()
            self.buffer_length_lock.acquire()
            print "buffer_length_lock acquired"
            self.buffer_length += int(segment['playback_length'])
            print "buffer_length:"
            print self.buffer_length
           # config_dash.LOG.debug("Replacing segment {} with higher quality segment".format(
           #     segment['playback_length']))

           # try:
           #     config_dash.LOG.info("{}: Started downloading segment {}".format(playback_type.upper(), segment_url))
           #     segment_size, segment_filename, segment_w_chunks = download_segment(segment_url, file_identifier)
           #     config_dash.LOG.info("{}: Finished Downloaded segment {}".format(playback_type.upper(), segment_url))
           # except IOError, e:
           #     config_dash.LOG.error("Unable to save segment %s" % e)
           #     return None
            self.buffer_length_lock.release()

        self.log_entry(action="Writing", bitrate=segment['bitrate'])
        print "-----------buffer:-----------"
        print self.buffer
        return self.buffer

    def start(self):
        """ Start playback"""
        self.set_state("INITIAL_BUFFERING")
        self.log_entry("Starting")
        config_dash.LOG.info("Starting the Player")
        self.player_thread = threading.Thread(target=self.initialize_player)
        self.player_thread.daemon = True
        self.player_thread.start()
        self.log_entry(action="Starting")

    def stop(self):
        """Method to stop the playback"""
        self.set_state("STOP")
        self.log_entry("Stopped")
        config_dash.LOG.info("Stopped the playback")

    def log_entry(self, action, bitrate=0):
        """Method to log the current state"""

        if self.buffer_log_file:
            header_row = None
            if self.actual_start_time:
                log_time = time.time() - self.actual_start_time
            else:
                log_time = 0
            if not os.path.exists(self.buffer_log_file):
                header_row = "EpochTime,CurrentPlaybackTime,CurrentBufferSize,CurrentPlaybackState,Action,Bitrate".split(",")
                # stats = (log_time, str(self.playback_timer.time()), self.buffer.qsize(),
                stats = (log_time, str(self.playback_timer.time()), self.buffer.__len__(), #MZ
                         self.playback_state, action,bitrate)
            else:
                stats = (log_time, str(self.playback_timer.time()), self.buffer.__len__(), #MZ
                         self.playback_state, action,bitrate)
            str_stats = [str(i) for i in stats]
            with open(self.buffer_log_file, "ab") as log_file_handle:
                result_writer = csv.writer(log_file_handle, delimiter=",")
                if header_row:
                    result_writer.writerow(header_row)
                result_writer.writerow(str_stats)
            config_dash.LOG.info("BufferStats: EpochTime=%s,CurrentPlaybackTime=%s,CurrentBufferSize=%s,"
                                 "CurrentPlaybackState=%s,Action=%s,Bitrate=%s" % tuple(str_stats))
            # buffer_size = self.buffer.qsize() * 2
            buffer_size = self.buffer.__len__() * 2
            with open("buffer.txt", "a") as bufstat:
                bufstat.write(str(log_time) + "\t" + str(buffer_size) + "\n")
