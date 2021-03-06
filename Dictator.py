#!/usr/bin/env python

#test on http://www.youtube.com/watch?v=Or5R_uPvPao


from sys import stdin, stdout
from time import time, sleep
from struct import unpack
from os.path import isfile, isdir
from os import mkdir, system
from Queue import Queue
from cStringIO import StringIO
from threading import Thread
from subprocess import call, Popen, PIPE	#note: the pexpect module looks interesting also!
from requests import get, post, adapters
from json import loads


#Argument parsing stuff
import argparse
parser = argparse.ArgumentParser(description='Realtime speech to text translation (and back)')

parser.add_argument('-l', '--log'      , help='output logging info for debuging purposes', action='store_true')
parser.add_argument('-v', '--verbose'  , help='output more text', action='store_true')

recorderGroup = parser.add_argument_group('Recorder opions')
recorderGroup.add_argument('-r', '--recorder', help='set recorder (input) method', choices=['arecord', '-', 'test1', 'frequent'], default='arecord')

convertorGroup = parser.add_argument_group('Convertor options')
#XXX +store input somewhere
convertorGroup.add_argument('-c', '--convertor', help='set raw input to flac conversion method', choices=['flac', 'sox'], default='flac')
convertorGroup.add_argument('-os', '--outputsamples'   , help='output speech samples (to /tmp/)', action='store_true')

sttGroup = parser.add_argument_group('Speech to text options')
sttGroup.add_argument('-sttv', '--sttvoice' , help='set voice' , default='en')
sttGroup.add_argument('-stte', '--sttengine', help='set engine', choices=['google'], default='google')
sttGroup.add_argument('-sttc', '--sttconfidence', help='set minimum confidence (default=0.8)', type=float, default=0.8)
sttGroup.add_argument('-sttunknowns', '--sttshowunknowns', help='display unknowns texts', action='store_true')

ttsGroup = parser.add_argument_group('Text to speech options')
ttsGroup.add_argument('-notts' , '--notexttospeech', help='disable text to speech', action='store_true', default=False)
ttsGroup.add_argument('-ttsc', '--ttsengine', help='set text to speech method', choices=['google',], default='google')
ttsGroup.add_argument('-ttsv', '--ttsvoice'    , help='set playback voice', default='en-us')
ttsGroup.add_argument('-ttsp', '--ttsplayer'   , help='set playback method', choices=['mplayer',], default='mplayer')

args = parser.parse_args()


#work around issues...
adapters.DEFAULT_RETRIES = 5	#prefends ConnectionErrors by urllib3 (used by requests)
QUIT_TTS_THREAD = '!@#$'
STT_UNKNOWN     = u'?'
LOW_CONFIDENCE_MARKER = '?'


#Global data
nSpeechToTextRequestsPending = 0
speechToTextResponseQueue = Queue()

speechToTextResponsesProcessed = 0
speechToTextResponses = {}
speechToTextLenFlacData = {}

ttsQueue = Queue()


#Helper functions
def log(s = ''):
	if args.log:
		stdout.write(s + '\n')	#uncomment this line to enable loging to stdout
		stdout.flush()
	return


#
#This runs in many seperate threads
#
def	speechToTextThread(counter, sampledata, flacFilename):

	#
	# 1. Convert the raw sample data to a flac file (requirement for the step 4)
	#
	if args.convertor == 'flac':
		convertor = convertorUsingFlac
	elif args.convertor == 'sox':
		convertor = convertorUsingSox

	#bufsize -1=system default bufsize, 0=no buffering
	#convertorProcess = Popen(convertor, bufsize=-1, stdin=PIPE, stdout=PIPE, stderr=PIPE)
	convertorProcess = Popen(convertor, stdin=PIPE, stdout=PIPE, stderr=PIPE)
	convertorProcess.stdin.write(sampledata.read())
	convertorProcess.stdin.close()
	flacdata = convertorProcess.stdout.read()

	sampledata.reset()
	log('%d bytes of samples -> %d bytes of flac data' % (len(sampledata.read()), len(flacdata)))

	#
	# 2. Optionally save the flac file
	#
	if flacFilename:
		log('Writting %s' % flacFilename)
		f = open(flacFilename, 'wb')
		f.write(flacdata)
		f.close()

	#
	# 3. Convert the speech sample to text
	#
	if args.sttengine == 'google':
		url     = 'http://www.google.com/speech-api/v1/recognize?lang=%s&client=chromium' % args.sttvoice
		headers = {'Content-Type': 'audio/x-flac; rate=16000'}
		files   = {'file': flacdata}
		r       = post(url, files=files, headers=headers)
		try:
			j    = loads(r.text)
			assert len(j['hypotheses']) == 1
			confidence = float( j['hypotheses'][0]['confidence'] )
			text = j['hypotheses'][0]['utterance']	#note: perhaps only when 'confidence' is high
			if confidence < args.sttconfidence:
				text = '%s%s%s %.2f' % (LOW_CONFIDENCE_MARKER, text, LOW_CONFIDENCE_MARKER, confidence)
			#print j
		except ValueError:
			text = STT_UNKNOWN

	#
	# 4. put the resulting text in a queue for later processing (because these responses might return out of order)
	#
	speechToTextResponseQueue.put( (counter, text) )


#
#
#
def	processSpeechToTextResponse():
	global	nSpeechToTextRequestsPending, speechToTextResponsesProcessed

	counter, text = speechToTextResponseQueue.get()
	speechToTextResponses[counter] = text
	nSpeechToTextRequestsPending -= 1

	while speechToTextResponses.has_key(speechToTextResponsesProcessed):
		s = speechToTextResponses[speechToTextResponsesProcessed]
		if s != STT_UNKNOWN or args.sttshowunknowns:
			if args.verbose:
				print '%4d. %s' % (speechToTextResponsesProcessed, s)
				#print '%4d. (%6d samples) %s' % (speechToTextResponsesProcessed, speechToTextLenFlacData[speechToTextResponsesProcessed], s)
				stdout.flush()
			else:
				print '%s -' % s,
				stdout.flush()
				#if s:
				#	print '%s -' % s,
				#	stdout.flush()

			if args.notexttospeech is False:
				if s[0] == LOW_CONFIDENCE_MARKER:
					s = s.split(LOW_CONFIDENCE_MARKER)[1]
				ttsQueue.put(s)

		speechToTextResponsesProcessed += 1


#
#This runs in a seperate thread
#
def	textToSpeechThread():
	while True:
		text = ttsQueue.get(True)	#will block until something is available
		if text == QUIT_TTS_THREAD:
			break

		flacDirname  = 'cache/%s/%s' % (args.ttsengine, args.ttsvoice)
		flacFilename = '%s/%s.flac' % (flacDirname, text)
		if isfile(flacFilename):	#cached copy available
			log('Use cached flac for "%s"' % text)

			f = open(flacFilename, 'rb')
			flac = f.read()
			f.close()
		else:
			log('Download flac for "%s"' % text)

			if args.ttsengine == 'google':
				url  = 'http://translate.google.com/translate_tts?tl=%s&q=%s' % (args.ttsvoice, text)

			r    = get(url)
			flac = r.content

			if not isdir(flacDirname):
				mkdir(flacDirname)

			f = open(flacFilename, 'wb')	#Cache the newly downloaded translation
			f.write(r.content)
			f.close()

		cmd = 'mplayer -ao alsa -really-quiet -noconsolecontrols - < "%s" > /dev/null 2>&1' % flacFilename
		#log(cmd)
		system(cmd)
		#mplayer = Popen(cmd.split(), stdin=PIPE, stdout=PIPE, stderr=PIPE)
		#mplayer.stdin.write(flac)
		#mplayer.stdin.close()


#
#setup our child processes.
#
#   warning: use tempfile.TemporaryFile instead of PIPE on large expected child output!!!)
#            to avoid deadlocks (child hangs until it's stdout get read by parent)
#

# 1. a single child process that gives us input samples
inputUsingARecord = 'arecord -D plughw:1,0 -q -f cd -t raw -c 1 -r 16000'.split()


# 2. possible commands for converting raw data on stdin to a flac file on stdout
convertorUsingFlac = 'flac - --channels=1 --endian=little --sign=signed --sample-rate=16000 --bps=16 --force-raw-format -s'.split()

#    normalize to -5 db, trim start/end silence based on quietness threshold (silence 1 5 2%)
convertorUsingSox = 'sox -t raw -c 1 -L -e signed -r 16k -b 16 - -t flac - gain -n -5'.split() #make louder


#
#Create a single sample and let child processes handle the sample in the backgroun.
#
#   ie. - normalize and clean up sample
#	- convert it to the correct .flac format (Signed 16 bit Little Endian, Rate 16000 Hz, Mono)
#	- let google handle translation
#	- output translation text result
#
def	sample(recorderStdin, counter, flacFilename):

	bytesPerSample    = 2
	samplesPerSecond  = 16000
	silenceThreshold  = 600
	maxSamples        = int(samplesPerSecond * 5)	#max seconds recording time (not too high to avoid deadlocks)
	smallPerOfASecond = bytesPerSample * samplesPerSecond / 50
	silenceFadeInDuration = int(bytesPerSample * samplesPerSecond * 0.5)
	minSilentSamples  = int(samplesPerSecond * 0.6)	#duration of silence to seperate speech samples
	minSpeechSamples  = int(samplesPerSecond * 0.3)	#discard speech samples that are too short
	nSamples          = 0
	silenceFadeIn     = ''
	sampledata        = StringIO()

	log('Silence...')
	while True:
		while not speechToTextResponseQueue.empty():
			processSpeechToTextResponse()

		samples = recorderStdin.read(smallPerOfASecond)	#skip samples to reduce cpu usage during silent periods
		if not samples:
			return -1				#Stop processing audio samples
		sample      = samples[-bytesPerSample:]

		#XXX debugging why start of samples are recognized so bad
		#sampledata.write(samples)

		silenceFadeIn = silenceFadeIn[-silenceFadeInDuration:] + samples

		sampleAsInt = unpack('<h', sample)[0]
		if abs(sampleAsInt) >= silenceThreshold:	#end of silency detected
			#XXX debugging why start of samples are recognized so bad
			sampledata.write(silenceFadeIn)
			nSamples = len(samples) / bytesPerSample
			break

	log('Recording...')
	sampleAfterSilence = maxSamples
	while nSamples < sampleAfterSilence and nSamples < maxSamples:
		while not speechToTextResponseQueue.empty():
			processSpeechToTextResponse()

		samples = recorderStdin.read(smallPerOfASecond)
		if not samples:
			break	#process this (last) audio sample
		sample = samples[:bytesPerSample]

		sampleAsInt = unpack('<h', sample)[0]
		if abs(sampleAsInt) < silenceThreshold:	#silence
			if sampleAfterSilence == maxSamples:
				sampleAfterSilence = nSamples + minSilentSamples
		else:	#noise
			if sampleAfterSilence != maxSamples:
				sampleAfterSilence = maxSamples

		#XXX actually we do not need to write the final (silent) part. TODO: optimize this!
		sampledata.write(samples)
		nSamples += len(samples) / bytesPerSample

		#log('%d samples' % nSamples)

	realNSamples = nSamples - minSilentSamples
	log('Finished recording after %.1f seconds...' % (float(realNSamples) / samplesPerSecond,))

	if realNSamples < minSpeechSamples:
		log('Discard very short sample')
		return 0

	global	nSpeechToTextRequestsPending
	nSpeechToTextRequestsPending += 1
	speechToTextLenFlacData[counter] = realNSamples

	sampledata.reset()
	t = Thread(target = speechToTextThread, args = (counter, sampledata, flacFilename), name = 'speechToTextThread')
	t.start()

	return 1



def	allSamples():
	log('-- begin --')

	if args.notexttospeech is False:
		Thread(target = textToSpeechThread, name = 'textToSpeechThread').start()

	if args.recorder == '-':
		recorderFd = stdin
	elif args.recorder == 'arecord':
		recorderFd = Popen(inputUsingARecord, stdout=PIPE).stdout
	elif args.recorder == 'test1':
		recorderFd = open('test/the sea change - ernest hemingway.raw', 'rb')
	elif args.recorder == 'frequent':
		populateCacheWithFrequentWords()
		return

	nRequests = 0
	while True:
		if args.outputsamples:
			flacFilename = '/tmp/dictator-%04d.flac' % nRequests
		else:
			flacFilename = None

		n = sample(recorderFd, nRequests, flacFilename)
		if n < 0:
			break

		nRequests += n

	waitForFinalRequestsToBeProcessed()
	log('-- end --')


def	waitForFinalRequestsToBeProcessed():
	log('Waiting for final requests to be processed...')
	while nSpeechToTextRequestsPending > 0:
		processSpeechToTextResponse()


def	populateCacheWithFrequentWords():
	for i in range(100):
		ttsQueue.put( str(i) )

	for p in range(2, 10):
		for n in range(1, 10):
			i = n * 10 ** p
		        ttsQueue.put( str(i) )


if __name__ == '__main__':
	try:
		allSamples()
	except KeyboardInterrupt:
		while not ttsQueue.empty():	#cancel pending text to speech
			ttsQueue.get()

	#force tts Thread to exit quickly
	ttsQueue.put(QUIT_TTS_THREAD)	#Hack

