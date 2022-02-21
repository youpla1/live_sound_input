#GPLv3.0
#qdelamar, 2022-02

#just put this file in /path/to/addons/animation_nodes/nodes/sound/
#this was coded for v2.2 but also works with v2.1.4 (win & linux)

import bpy
from bpy.props import *
from animation_nodes.data_structures import Sound, SoundSequence, SoundData
from animation_nodes.events import propertyChanged
from animation_nodes.base_types import AnimationNode
from animation_nodes.data_structures.sounds.sound_sequence import sampleRate #harcoded here: guys seriously...
try:
    import sounddevice as sd
except ModuleNotFoundError:
    sounddevice_not_found = "You need the sounddevice python library to run the LiveInputSound node, try this:\n/path/to/blender/python/bin/python -m pip install sounddevice"
    raise ModuleNotFoundError(sounddevice_not_found)

import numpy as np #we need concatenate


MIN_WAVEPOINT_HISTORY = 2**15 #18375 #we typically have 44100/24=1837.5 wavepoints per render frame; let's keep >10 frames. The time smoothed spectrum typically needs several frames so don't set this to a too small value.


devices = sd.query_devices()
deviceItems = [(str(i), devices[i]["name"], devices[i]["name"], "", i) for i in range(len(devices)) if devices[i]["max_input_channels"]]


instance_streams = {} #instance:device id
shared_streams = {} #device id:(stream, number of users)
def updateDevice(self, context, close=0):
    global shared_streams, instance_streams

    #manage previous stream:
    selfid = hash(self) #node instance identifier
    if selfid in instance_streams: #if we had a stream open we may need to close it
        last_devid = instance_streams[selfid]
        shared_streams[last_devid][1] -= 1 #note: afaik the node tree execution is single threaded so there should not be a race here... for now
        if shared_streams[last_devid][1] == 0: #check if anyone else needs it
            print("[LiveSoundInput] Closing stream for the previous device ("+str(last_devid)+")")
            if shared_streams[last_devid][0]:
                shared_streams[last_devid][0].stop() #no, so close it
                global_rec_buffer[last_devid] = [] #empty the buffers

    if close: #this should be the case only when deleting the node
        if selfid in instance_streams:
            del instance_streams[selfid]
        return

    #manage new stream:
    devid = int(self.device_id)
    device = devices[devid]
    chans = device["max_input_channels"]
    print("[LiveSoundInput] Switching to device "+str(devid)+": '"+str(device["name"])+"'")
    if not devid in shared_streams:
        shared_streams[devid] = [None, 0]
    if shared_streams[devid][1] == 0: #nobody uses this stream, so open it
        print("[LiveSoundInput] Opening stream for this device")
        try:
            stream = sd.InputStream(samplerate=sampleRate, device=devid, channels=chans, dtype='float32', callback=lambda i,f,t,s:get_data(devid,i,f,t,s))
            stream.start()
        except Exception as err:
            print("[LiveSoundInput] Could not open stream:", err)
            stream = None
        shared_streams[devid][0] = stream #store it

    shared_streams[devid][1] += 1
    instance_streams[selfid] = devid

    propertyChanged(self, context) # propagate event


global_rec_buffer = {} # devid:[indata, indata_prev, ...]
def get_data(devid, indata, frames, time, status):
    global global_rec_buffer
    if not devid in global_rec_buffer:
        global_rec_buffer[devid] = []
    global_rec_buffer[devid].insert(0, indata) #prepend indata to the list of buffers

    #keep the minimum amount of buffers to have more or equal than the required number of points:
    i = len(global_rec_buffer[devid])
    lens = list(map(len, global_rec_buffer[devid]))
    while sum(lens[:i]) >= MIN_WAVEPOINT_HISTORY: #this should not be 0 ;)
        i -= 1
    global_rec_buffer[devid] = global_rec_buffer[devid][:i+1] #drop the old ones


class LiveSoundInput(bpy.types.Node, AnimationNode):
    bl_idname = "an_LiveSoundInput"
    bl_label = "Get live sound"

    device_id: EnumProperty(name="Audio device", default=deviceItems[0][0], description="The device used as audio source", items=deviceItems, update=lambda s,c:updateDevice(s,c,0))
    gain: FloatProperty(name="Gain", default=1.0, description="A gain applied to the sound", update=propertyChanged)
    frame_offset: IntProperty(name="Frame offset", default=0, description="An offset in the timestamp of the output Sound. This does not affect the float wavepoint output.", update=propertyChanged)
    mono: BoolProperty(name="Mono", default=True, description="Output a mono sound or all channels", update=AnimationNode.refresh)

    def create(self):
        self.newInput("Float", "Frame", "frame")
        updateDevice(self, None, 0) # initialize streams
        if self.mono:
            self.newOutput("Sound", "Live sound channel", "sound")
            self.newOutput("Float", "Wavepoints of channel", "sound_float")
        else:
            for i in range(devices[int(self.device_id)]["max_input_channels"]):
                self.newOutput("Sound", "Live sound channel "+str(i), "sound_"+str(i))
                self.newOutput("Float", "Wavepoints of channel "+str(i), "sound_float_"+str(i))

    def draw(self, layout):
        layout.prop(self, "device_id")
        layout.prop(self, "gain")
        #layout.prop(self, "frame_offset") #works well with this = 0, no need to expose to user for now
        layout.prop(self, "mono")

    def execute(self, frame):
        devid = int(self.device_id)
        device = devices[devid]
        #fs = device["default_samplerate"] #would need to resample if !=sampleRate
        chans = device["max_input_channels"]

        global global_rec_buffer #the audio data is stored in this global dict by the stream callbacks
        if not devid in global_rec_buffer:
            if self.mono:
                return Sound([]), 0
            else:
                out = []
                for ichan in range(chans):
                    out.extend([Sound([]), 0])
                return out

        #get previously recorded samples:
        buffers = global_rec_buffer[devid]
        if not buffers:
            if self.mono:
                return Sound([]), 0
            else:
                out = []
                for ichan in range(chans):
                    out.extend([Sound([]), 0])
                return out

        rec = np.concatenate(buffers[::-1]) #reorder chronologically
        nsamples = rec.shape[0]

        fps = bpy.context.scene.render.fps / bpy.context.scene.render.fps_base #typically 24
        #align the time range such that the end is now + 'frame_offset' frames in the future
        now_plus_offset = (frame+self.frame_offset)/fps #unit=seconds
        start_s = now_plus_offset - nsamples/sampleRate
        end_s = now_plus_offset
        nfir = int(2*sampleRate/fps)

        out = []
        outchans = chans
        if self.mono:
            outchans = 1
        for ichan in range(outchans):
            if self.mono:
                raw_data = sum(rec.T)/chans*self.gain
            else:
                raw_data = rec.T[ichan,:]*self.gain

            #build the Sound object:
            data = SoundData(raw_data, sampleRate)
            try:
                soundSequence = SoundSequence(data, start=start_s, end=end_s, volume=1, fps=fps, startOffset=0)
            except TypeError: #handle old versions of the animation node addon
                soundSequence = SoundSequence(data, start=start_s, end=end_s, volume=1, fps=fps)

            sound = Sound([soundSequence])

            #for those who want to play directly with the audio wavepoints:
            sound_float = sum(raw_data[-nfir:])/nfir #simple rectangular AAF with a 0 at the nyquist frequency

            out.extend([sound, sound_float])
        return out

    def delete(self):
        #properly close any open stream if needed:
        updateDevice(self, None, close=1)
