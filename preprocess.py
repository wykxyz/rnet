#!/usr/bin/env python

# coding=utf-8
import os
import re
import tensorflow as tf
from collections import Counter
import collections
import json
import numpy as np
import nltk
import threading
nltk.download('punkt')
from nltk.tokenize import word_tokenize

class DataProcessor:
    def __init__(self, data_type, opt):
        self.data_type = data_type
        self.opt = opt
        self.source_path = os.path.join('data', self.data_type+'-v1.1.json')
        self.sink_path = os.path.join('data', 'processed_'+self.data_type+'-v1.1.json')
        self.glove_path = os.path.join('data', 'glove.'+self.opt['token_size']+'.'+str(self.opt['emb_dim'])+'d.txt')
        self.share_path = os.path.join('data', 'share.'+self.opt['token_size']+'.'+str(self.opt['emb_dim'])+'d.txt')
        self.batch_size = opt['batch_size']
        self.p_length = opt['p_length']
        self.q_length = opt['q_length']
        self.emb_dim = opt['emb_dim']
        self.queue_size = opt['queue_size']
        self.num_threads = opt['num_threads']
        self.read_batch = opt['read_batch']
        self.no = 0
        self.no_lock = threading.Lock()

    def process(self):
        '''
        pre-process the data
        1. tokenize all paragraphs, questions, answers
        2. find embedding for all words that showed up
        3. stored
        '''
        with open(self.source_path, 'r') as source_file:
            source_data = json.load(source_file)
            sink_data = []

            n_article = len(source_data['data'])
            # memorize all words and create embedding efficiently
            word_map = set()
            articles = []
            for ai, article in enumerate(source_data['data']):
                paragraphs = []
                if ai%10 == 0:
                    print('processing article {}/{}'.format(ai, n_article))
                for pi, p in enumerate(article['paragraphs']):
                    context = p['context']
                    context_words = word_tokenize(context)
                    paragraphs.append(context_words)
                    num_words = len(context_words)
                    for w in context_words:
                        word_map.add(w)

                    for qa in p['qas']:
                        question_words = word_tokenize(qa['question'])

                        # only care about the first answer
                        a = qa['answers'][0]
                        answer = a['text'].strip()
                        answer_start = int(a['answer_start'])
                        answer_words = word_tokenize(answer)

                        # need to find word level idx
                        w_start = len(word_tokenize(context[:answer_start]))
                        answer_idx = [i + w_start for i in range(len(answer_words)) if i + w_start < num_words]
                        si, ei = answer_idx[0], answer_idx[-1]

                        sample = {
                            'ai': ai,
                            'pi': pi,
                            'question': question_words,
                            'answer': answer_words,
                            'si': si,
                            'ei': ei,
                            'id': qa['id']
                            }
                        sink_data.append(sample)
                articles.append(paragraphs) 

        w2v = self.get_word_embedding(word_map)
        share_data = {
            'w2v': w2v,
            'articles': articles
        }
        print('Saving...')
        with open(os.path.join('data', self.share_path), 'w') as f:
            json.dump(share_data, f)
        with open(os.path.join('data', self.sink_path), 'w') as f:
            json.dump(sink_data, f)
        print('SQuAD '+self.data_type+' preprossing finished!')


    def get_word_embedding(self, word_map):
        print('generating embedding')
        word2vec = {}
        with open(self.glove_path, 'r', encoding='utf-8') as fn:
            for line in fn:
                array = line.strip().split(' ')
                w = array[0]
                v = list(map(float, array[1:]))
                if w in word_map:
                    word2vec[w] = v
                if w.capitalize() in word_map:
                    word2vec[w.capitalize()] = v
                if w.lower() in word_map:
                    word2vec[w.lower()] = v
                if w.upper() in word_map:
                    word2vec[w.upper()] = v
        print("{}/{} of word vocab have corresponding vectors in {}".format(len(word2vec), len(word_map), self.glove_path))
        return word2vec

    def load_and_enqueue(self, sess, enqueue_op, coord):
        '''
        enqueues training sample, per read_batch per time 
        '''
        assert(self.sink_data)
        assert(self.share_data)
        while not coord.should_stop():
            self.no_lock.acquire()
            start_idx = self.no
            end_idx = min(self.num_sample, self.no + self.read_batch)
            self.no = end_idx
            self.no_lock.release()
            w2v_table = self.share_data['w2v']
            p = np.zeros((self.p_length, self.emb_dim))
            q = np.zeros((self.q_length, self.emb_dim))
            asi = np.zeros((self.p_length))
            aei = np.zeros((self.p_length))
            for i in range(start_idx, end_idx):
                sample = self.sink_data[i]
                question = sample['question']
                paragraph = self.share_data['article'][sample['ai']][sample['pi']]
                for j in range(min(len(paragraph), self.p_length)):
                    try:
                        p[j] = w2v_table[paragraph[j]]
                    except KeyError:
                        pass
                for j in range(min(len(question), self.q_length)):
                    try:
                        q[j] = w2v_table[question[j]]
                    except KeyError:
                        pass
                asi[sample['si']] = 1.0
                aei[sample['ei']] = 1.0
                sess.run(enqueue_op, feed_dict={self.it['eP']: p, self.it['eQ']: q, self.it['asi']: asi, self.it['aei']: aei})
        

    def provide(self, sess):
        '''
        creates enqueue and dequeue operations to build input pipeline
        '''
        with open(self.sink_path, 'r') as data_raw, open(self.share_path, 'r') as share_raw:
            self.sink_data = json.load(data_raw)
            self.share_data = json.load(share_raw)
            self.num_sample = len(self.sink_data)
        eP = tf.placeholder(tf.float32, [self.p_length, self.emb_dim])
        eQ = tf.placeholder(tf.float32, [self.q_length, self.emb_dim])
        asi = tf.placeholder(tf.float32, [self.p_length])
        aei = tf.placeholder(tf.float32, [self.p_length])
        self.it = {'eP': eP, 'eQ': eQ, 'asi': asi, 'aei': aei}
        with tf.variable_scope("queue"):
            q = tf.FIFOQueue(self.queue_size, [tf.float32, tf.float32, tf.float32, tf.float32], shapes=[[self.p_length, self.emb_dim], [self.q_length, self.emb_dim], [self.p_length], [self.p_length]])
            enqueue_op = q.enqueue([eP, eQ, asi, aei])
            # qr = tf.train.QueueRunner(q, [enqueue_op] * self.num_threads)
            # tf.train.add_queue_runner(qr)
            eP_batch, eQ_batch, asi_batch, aei_batch = q.dequeue_many(self.batch_size)
            
        input_pipeline = {
            'eP': eP_batch,
            'eQ': eQ_batch,
            'asi': asi_batch,
            'aei': aei_batch
        }
        return input_pipeline, enqueue_op
        

def read_data(data_type, opt):
    return DataProcessor(data_type, opt)

def run():
    opt = json.load(open('model/config.json', 'r'))['rnet']
    dp = DataProcessor('train', opt)
    dp.process()

if __name__ == "__main__":
	run()
