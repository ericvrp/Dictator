#!/usr/bin/env python

#test on http://www.youtube.com/watch?v=Or5R_uPvPao


from sys import stdin, stdout
from time import time, sleep
from struct import unpack
from Queue import PriorityQueue
from threading import Thread
from subprocess import Popen, PIPE	#note: the pexpect module looks interesting also!
from requests import post, adapters
from json import loads


#Argument parsing stuff
import argparse
parser = argparse.ArgumentParser(description='Realtime speech to text translation (and back)')

parser.add_argument('-l', '--log'      , help='output logging info for debuging purposes', action='store_true')
parser.add_argument('-v', '--verbose'  , help='output more text', action='store_true')

recorderGroup = parser.add_argument_group('Recorder opions')
recorderGroup.add_argument('-r', '--recorder', help='set recorder (input) method', choices=['-', 'arecord', 'test1'], default='arecord')

convertorGroup = parser.add_argument_group('Convertor options')
#XXX +store input somewhere
convertorGroup.add_argument('-c', '--convertor', help='set raw input to flac conversion method', choices=['flac', 'sox'], default='flac')
convertorGroup.add_argument('-os', '--outputsamples'   , help='output speech samples (to /tmp/)', action='store_true')

sttGroup = parser.add_argument_group('Speech to text options')
#XXX +speech to text translator (google)
#XXX +speech to text minimal confidence (0.9 ??)
#XXX +display repetitive unknown translations (boolean default=False)

ttsGroup = parser.add_argument_group('Text to speech options')
#XXX +text to speech translator (google)
ttsGroup.add_argument('-tts', '--ttsconvertor', help='set text to speech method', default='google')
ttsGroup.add_argument('-ttsv', '--ttsvoice'   , help='set voice for playback', default='en')
ttsGroup.add_argument('-ttsp', '--ttsplay'    , help='set playback method', choices=['none', 'mplayer'], default='none')

args = parser.parse_args()


#work around issues...
adapters.DEFAULT_RETRIES = 5	#prefends ConnectionErrors by urllib3 (used by requests)


#Global data
nSpeechToTextRequestsPending = 0
speechToTextResponseQueue = PriorityQueue()

speechToTextResponsesProcessed = 0
speechToTextResponses = {}
speechToTextLenFlacData = {}


#Helper functions
def log(s = ''):
	if args.log:
		stdout.write(s + '\n')	#uncomment this line to enable loging to stdout
		stdout.flush()
	return


def	speechToText(counter, flacdata):
	url     = 'http://www.google.com/speech-api/v1/recognize?lang=en-us&client=chromium'
	headers = {'Content-Type': 'audio/x-flac; rate=16000'}
	files   = {'file': flacdata}

	r = post(url, files=files, headers=headers)
	#print r.status_code
	#print r.headers
	#print r.content
	#print r.text

	try:
		j    = loads(r.text)
		assert len(j['hypotheses']) == 1
		text = j['hypotheses'][0]['utterance']	#note: perhaps only when 'confidence' is high
		#print j
	except ValueError:
		text= u'...'

	speechToTextResponseQueue.put( (counter, text) )


def	processSpeechToTextResponse():
	global	nSpeechToTextRequestsPending, speechToTextResponsesProcessed

	counter, text = speechToTextResponseQueue.get()
	speechToTextResponses[counter] = text
	nSpeechToTextRequestsPending -= 1

	while speechToTextResponses.has_key(speechToTextResponsesProcessed):
		if args.verbose:
			print '%4d. %s' % (speechToTextResponsesProcessed, speechToTextResponses[speechToTextResponsesProcessed])
			#print '%4d. (%6d samples) %s' % (speechToTextResponsesProcessed, speechToTextLenFlacData[speechToTextResponsesProcessed], speechToTextResponses[speechToTextResponsesProcessed])
			stdout.flush()
		else:
			if speechToTextResponses[speechToTextResponsesProcessed]:
				print '%s -' % (speechToTextResponses[speechToTextResponsesProcessed],),
				stdout.flush()
		speechToTextResponsesProcessed += 1


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
def	sample(recorderStdin, convertorStdin, convertorStdout, counter, flacTmpFilename=None):

	bytesPerSample    = 2
	samplesPerSecond  = 16000
	silenceThreshold  = 500
	maxSamples        = int(samplesPerSecond * 5)	#max seconds recording time (not too high to avoid deadlocks)
	smallPerOfASecond = bytesPerSample * samplesPerSecond / 100
	minSilentSamples  = int(samplesPerSecond * 0.5)	#duration of silence to seperate speech samples
	minSpeechSamples  = int(samplesPerSecond * 0.3)	#discard speech samples that are too short
	nSamples          = 0

	log('Silence...')
	while True:
		while not speechToTextResponseQueue.empty():
			processSpeechToTextResponse()

		samples = recorderStdin.read(smallPerOfASecond)	#skip samples to reduce cpu usage during silent periods
		if not samples:
			return -1				#Stop processing audio samples
		sample      = samples[-bytesPerSample:]
		sampleAsInt = unpack('<h', sample)[0]
		if abs(sampleAsInt) >= silenceThreshold:	#end of silency detected
                	convertorStdin.write(samples)		#output because we don't know where the noise started
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
		convertorStdin.write(samples)	#deadlocks after ~122Ksamples because convertor waits for all data
		nSamples += len(samples) / bytesPerSample

		#log('%d samples' % nSamples)

	realNSamples = nSamples - minSilentSamples
	log('Finished recording after %.1f seconds...' % (float(realNSamples) / samplesPerSecond,))

	#read flacfile from convertorStdout (XXX better in above loop to avoid deadlock when convertorStdout gets full)
	convertorStdin.close()

	if realNSamples < minSpeechSamples:
		log('Discard very short sample')
		return 0

	if flacTmpFilename:
		log('Writting %s' % flacTmpFilename)
		flacFile = convertorStdout.read()	#actually not to read the flacfile here
		f = open(flacTmpFilename, 'wb')
		f.write(flacFile)
		f.close()
	else:
		flacFile = convertorStdout		#we avoid reading it early here

	global	nSpeechToTextRequestsPending
	nSpeechToTextRequestsPending += 1
	speechToTextLenFlacData[counter] = realNSamples
	t = Thread(target=speechToText, args=(counter, flacFile))
	t.start()

	return 1



def	allSamples():
	log('-- begin --')

	if args.recorder == '-':
		recorderFd = stdin
	elif args.recorder == 'arecord':
		recorderFd = Popen(inputUsingARecord, stdout=PIPE).stdout
	elif args.recorder == 'test1':
		recorderFd = open('test/the sea change - ernest hemingway.raw', 'rb')

	if args.convertor == 'flac':
		convertor = convertorUsingFlac
	elif args.convertor == 'sox':
		convertor = convertorUsingSox

	nRequests = 0
	while True:
		#XXX look into multithread the convertor process because using pipes doesn't seem to parallelize

		#bufsize -1=system default bufsize, 0=no buffering
		convertorProcess = Popen(convertor, bufsize=-1, stdin=PIPE, stdout=PIPE)

		if args.outputsamples:
			flacFilename = '/tmp/dictator-%04d.flac' % nRequests
		else:
			flacFilename = None

		n = sample(recorderFd, convertorProcess.stdin, convertorProcess.stdout, nRequests, flacFilename)
		if n < 0:
			break

		nRequests += n

	log('Waiting for final requests to be processed...')
	while nSpeechToTextRequestsPending > 0:
		processSpeechToTextResponse()

	log('-- end --')


if __name__ == '__main__':
	allSamples()

