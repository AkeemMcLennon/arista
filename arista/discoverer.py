# -*- Mode: Python -*-
# vi:si:et:sw=4:sts=4:ts=4

# discoverer.py
# (c) 2005 Edward Hervey <edward at fluendo dot com>
# Discovers multimedia information on files

# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or (at your option) any later version.
# 
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
# 
# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, write to the Free Software
# Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301  USA

"""
Class and functions for getting multimedia information about files

Modified to support dvd://device@title style URIs using dvdreadsrc.
Modified to support v4l://device style URIs using v4lsrc.
Modified to support v4l2://device style URIs using v4l2src.

Modified to use uridecodebin instead of decodebin
"""

import gettext
import logging

import os.path

import gobject

import gst

from gst.extend.pygobject import gsignal

_ = gettext.gettext
_log = logging.getLogger("arista.discoverer")

class Discoverer(gst.Pipeline):
    """
    Discovers information about files.
    This class is event-based and needs a mainloop to work properly.
    Emits the 'discovered' signal when discovery is finished.

    The 'discovered' callback has one boolean argument, which is True if the
    file contains decodable multimedia streams.
    """
    __gsignals__ = {
        'discovered' : (gobject.SIGNAL_RUN_FIRST,
                        None,
                        (gobject.TYPE_BOOLEAN, ))
        }
    
    mimetype = None

    audiocaps = {}
    videocaps = {}

    videowidth = 0
    videoheight = 0
    videorate = 0

    audiofloat = False
    audiorate = 0
    audiodepth = 0
    audiowidth = 0
    audiochannels = 0

    audiolength = 0L
    videolength = 0L

    is_video = False
    is_audio = False

    otherstreams = []

    finished = False
    sinknumber = 0
    tags = {}


    def __init__(self, filename, max_interleave=1.0):
        """
        filename: str; absolute path of the file to be discovered.
        max_interleave: int or float; the maximum frame interleave in seconds.
            The value must be greater than the input file frame interleave
            or the discoverer may not find out all input file's streams.
            The default value is 1 second and you shouldn't have to change it,
            changing it mean larger discovering time and bigger memory usage.
        """
        gobject.GObject.__init__(self)
        
        self.filename = filename
        
        self.mimetype = None

        self.audiocaps = {}
        self.videocaps = {}

        self.videowidth = 0
        self.videoheight = 0
        self.videorate = gst.Fraction(0,1)

        self.audiofloat = False
        self.audiorate = 0
        self.audiodepth = 0
        self.audiowidth = 0
        self.audiochannels = 0

        self.audiolength = 0L
        self.videolength = 0L

        self.is_video = False
        self.is_audio = False

        self.otherstreams = []

        self.finished = False
        self.tags = {}
        self._success = False
        self._nomorepads = False

        self._timeoutid = 0
        self._max_interleave = max_interleave
        
        self.dbin = None
        if filename.startswith("dvd://"):
            parts = filename.split("@")
            if len(parts) > 1:
                # Specific chapter was requested, so we need to use a different
                # source to manually specify the title to decode.
                self.src = gst.element_factory_make("dvdreadsrc")
                self.src.set_property("device", parts[0][6:])
                self.src.set_property("title", int(parts[1]))
                self.dbin = gst.element_factory_make("decodebin2")
                
                self.add(self.src, self.dbin)
                self.src.link(self.dbin)
                
                self.typefind = self.dbin.get_by_name("typefind")
                self.typefind.connect("have-type", self._have_type_cb)
                
                self.dbin.connect("new-decoded-pad", self._new_decoded_pad_cb)
                self.dbin.connect("no-more-pads", self._no_more_pads_cb)
        elif filename.startswith("v4l://"):
            pass
        elif filename.startswith("v4l2://"):
            pass
        elif filename.startswith("file://"):
            pass
        else:
            filename = "file://" + os.path.abspath(filename)
        
        if not self.dbin:
            # No custom source was setup, so let's use the uridecodebin!
            self.dbin = gst.element_factory_make("uridecodebin")
            self.dbin.set_property("uri", filename)
            self.add(self.dbin)

            self.dbin.connect("element-added", self._element_added_cb)
            self.dbin.connect("pad-added", self._new_decoded_pad_cb)
            self.dbin.connect("no-more-pads", self._no_more_pads_cb)

    @property
    def length(self):
        return max(self.videolength, self.audiolength)

    def _element_added_cb(self, bin, element):
        try:
            typefind = element.get_by_name("typefind")
            if typefind:
                self.typefind = typefind
                self.typefind.connect("have-type", self._have_type_cb)
            
            try:
                element.connect("unknown-type", self._unknown_type_cb)
            except TypeError:
                # Element doesn't support unknown-type signal?
                pass
        except AttributeError:
            # Probably not the decodebin, just ignore
            pass

    def _timed_out_or_eos(self):
        if (not self.is_audio and not self.is_video) or \
                (self.is_audio and not self.audiocaps) or \
                (self.is_video and not self.videocaps):
            self._finished(False)
        else:
            self._finished(True)

    def _finished(self, success=False):
        self.debug("success:%d" % success)
        self._success = success
        self.bus.remove_signal_watch()
        if self._timeoutid:
            gobject.source_remove(self._timeoutid)
            self._timeoutid = 0
        gobject.idle_add(self._stop)
        return False

    def _stop(self):
        self.debug("success:%d" % self._success)
        self.finished = True
        self.set_state(gst.STATE_READY)
        self.debug("about to emit signal")
        self.emit('discovered', self._success)

    def _bus_message_cb(self, bus, message):
        if message.type == gst.MESSAGE_EOS:
            self.debug("Got EOS")
            self._timed_out_or_eos()
        elif message.type == gst.MESSAGE_TAG:
            for key in message.parse_tag().keys():
                self.tags[key] = message.structure[key]
        elif message.type == gst.MESSAGE_ERROR:
            self.debug("Got error")
            self._finished()

    def discover(self):
        """Find the information on the given file asynchronously"""
        _log.debug(_("Discovering %(filename)s") % {
            "filename": self.filename
        })
        self.debug("starting discovery")
        if self.finished:
            self.emit('discovered', False)
            return

        self.bus = self.get_bus()
        self.bus.add_signal_watch()
        self.bus.connect("message", self._bus_message_cb)

        # 3s timeout
        self._timeoutid = gobject.timeout_add(3000, self._timed_out_or_eos)
        
        self.info("setting to PLAY")
        if not self.set_state(gst.STATE_PLAYING):
            self._finished()

    def _time_to_string(self, value):
        """
        transform a value in nanoseconds into a human-readable string
        """
        ms = value / gst.MSECOND
        sec = ms / 1000
        ms = ms % 1000
        min = sec / 60
        sec = sec % 60
        return "%2dm %2ds %3d" % (min, sec, ms)

    def print_info(self):
        """prints out the information on the given file"""
        if not self.finished or not (self.is_audio or self.is_video):
            return
        print _("Mime Type :\t"), self.mimetype
        if not self.is_video and not self.is_audio:
            return
        print _("Length :\t"), self._time_to_string(max(self.audiolength, self.videolength))
        print _("\tAudio:"), self._time_to_string(self.audiolength), _("\n\tVideo:"), self._time_to_string(self.videolength)
        if self.is_video and self.videorate:
            print _("Video :")
            print _("\t%(width)d x %(height)d @ %(rate_num)d/%(rate_den)d fps") % {
                "width": self.videowidth,
                "height": self.videoheight,
                "rate_num": self.videorate.num,
                "rate_den": self.videorate.denom
            }
            if self.tags.has_key("video-codec"):
                print _("\tCodec :"), self.tags.pop("video-codec")
        if self.is_audio:
            print _("Audio :")
            if self.audiofloat:
                print _("\t%(channels)d channels(s) : %(rate)dHz @ %(width)dbits (float)") % {
                    "channels": self.audiochannels,
                    "rate": self.audiorate,
                    "width": self.audiowidth
                }
            else:
                print _("\t%(channels)d channels(s) : %(rate)dHz @ %(depth)dbits (int)") % {
                    "channels": self.audiochannels,
                    "rate": self.audiorate,
                    "depth": self.audiodepth
                }
            if self.tags.has_key("audio-codec"):
                print _("\tCodec :"), self.tags.pop("audio-codec")
        for stream in self.otherstreams:
            if not stream == self.mimetype:
                print _("Other unsuported Multimedia stream :"), stream
        if self.tags:
            print _("Additional information :")
            for tag in self.tags.keys():
                print "%20s :\t" % tag, self.tags[tag]

    def _no_more_pads_cb(self, dbin):
        self.info("no more pads")
        self._nomorepads = True

    def _unknown_type_cb(self, dbin, pad, caps):
        self.debug("unknown type : %s" % caps.to_string())
        # if we get an unknown type and we don't already have an
        # audio or video pad, we are finished !
        self.otherstreams.append(caps.to_string())
        if not self.is_video and not self.is_audio:
            self.finished = True
            self._finished()

    def _have_type_cb(self, typefind, prob, caps):
        self.mimetype = caps.to_string()

    def _notify_caps_cb(self, pad, args):
        caps = pad.get_negotiated_caps()
        if not caps:
            pad.info("no negotiated caps available")
            return
        pad.info("caps:%s" % caps.to_string())
        # the caps are fixed
        # We now get the total length of that stream
        q = gst.query_new_duration(gst.FORMAT_TIME)
        pad.info("sending position query")
        if pad.get_peer().query(q):
            format, length = q.parse_duration()
            pad.info("got position query answer : %d:%d" % (length, format))
        else:
            length = -1
            gst.warning("position query didn't work")

        # We store the caps and length in the proper location
        if "audio" in caps.to_string():
            self.audiocaps = caps
            self.audiolength = length
            self.audiorate = caps[0]["rate"]
            self.audiowidth = caps[0]["width"]
            self.audiochannels = caps[0]["channels"]
            if "x-raw-float" in caps.to_string():
                self.audiofloat = True
            else:
                self.audiodepth = caps[0]["depth"]
            if self._nomorepads and ((not self.is_video) or self.videocaps):
                self._finished(True)
        elif "video" in caps.to_string():
            self.videocaps = caps
            self.videolength = length
            self.videowidth = caps[0]["width"]
            self.videoheight = caps[0]["height"]
            self.videorate = caps[0]["framerate"]
            if self._nomorepads and ((not self.is_audio) or self.audiocaps):
                self._finished(True)

    def _new_decoded_pad_cb(self, dbin, pad, extra=None):
        # Does the file contain got audio or video ?
        caps = pad.get_caps()
        gst.info("caps:%s" % caps.to_string())
        if "audio" in caps.to_string():
            self.is_audio = True
        elif "video" in caps.to_string():
            self.is_video = True
        else:
            self.warning("got a different caps.. %s" % caps.to_string())
            return
        #if is_last and not self.is_video and not self.is_audio:
        #    self.debug("is last, not video or audio")
        #    self._finished(False)
        #    return
        # we connect a fakesink to the new pad...
        pad.info("adding queue->fakesink")
        fakesink = gst.element_factory_make("fakesink", "fakesink%d-%s" % 
            (self.sinknumber, "audio" in caps.to_string() and "audio" or "video"))
        self.sinknumber += 1
        queue = gst.element_factory_make("queue")
        # we want the queue to buffer up to the specified amount of data 
        # before outputting. This enables us to cope with formats 
        # that don't create their source pads straight away, 
        # but instead wait for the first buffer of that stream.
        # The specified time must be greater than the input file
        # frame interleave for the discoverer to work properly.
        queue.props.min_threshold_time = int(self._max_interleave * gst.SECOND)
        queue.props.max_size_time = int(2 * self._max_interleave * gst.SECOND)
        queue.props.max_size_bytes = 0

        # If durations are bad on the buffers (common for video decoders), we'll
        # never reach the min_threshold_time or max_size_time. So, set a
        # max size in buffers, and if reached, disable the min_threshold_time.
        # This ensures we don't fail to discover with various ffmpeg 
        # demuxers/decoders that provide bogus (or no) duration.
        queue.props.max_size_buffers = int(100 * self._max_interleave)
        def _disable_min_threshold_cb(queue):
            queue.props.min_threshold_time = 0
            queue.disconnect(signal_id)
        signal_id = queue.connect('overrun', _disable_min_threshold_cb)

        self.add(fakesink, queue)
        queue.link(fakesink)
        sinkpad = fakesink.get_pad("sink")
        queuepad = queue.get_pad("sink")
        # ... and connect a callback for when the caps are fixed
        sinkpad.connect("notify::caps", self._notify_caps_cb)
        if pad.link(queuepad):
            pad.warning("##### Couldn't link pad to queue")
        queue.set_state(gst.STATE_PLAYING)
        fakesink.set_state(gst.STATE_PLAYING)
        gst.info('finished here')
