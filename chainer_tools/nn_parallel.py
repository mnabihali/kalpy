# -*- coding: utf-8 -*-
"""
Created on Thu Dec 10 18:57:42 2015

@author: tawara
"""
from cuda_tools.cuda_utils import SelectGpuIdAuto
from kaldi.data import load_nnet, convert_nnet_to_conf

import chainer.serializers
from chainer import Variable, cuda, optimizers
from chainer import Chain, ChainList
from chainer.serializers import HDF5Serializer,HDF5Deserializer
import chainer.functions as F
import chainer.links as L

import copy
import numpy as np
import h5py

from my_softmax_cross_entropy import softmax_cross_entropy
import my_batch_normalization
reload(my_batch_normalization)
from my_batch_normalization import BatchNormalization
from max_unpooling_2d import max_unpooling_2d



# # Define NN
class NN_parallel():
    '''
    This class contains Neuralnetwork, corresponding structure, and optimizer to optimize it.
    The purpose of this container is provide easy interface including IO and parallelization frameworks
    to chainer-based NN.

    This class supports Kaldi nnet1 format and HDF5 to save model structure and its paramter.

    *** The current version doesn't implement any parallelization yet. So njobs must be -1(cpu) or 1 ***
    '''
    def __init__(self, specs, njobs=-1):
        '''
        specs is dict which contains model structure.
        njobs is a number of GPUs to be used. 
        In this function, free GPU(s) with the largest available memory are automatically selected.
        If njobs = -1, no GPU is selected and run CPU mode.
        '''
        self.specs = specs
        if specs.has_key("layers_specs"):
            layers_specs = self.specs["layers_specs"]
            self.layers = CNN(layers_specs)
        if specs.has_key("optimizer"):
            self.set_optimizer(self.specs["optimizer"])

        if njobs==-1:
            self.device_id = [-1] # cpu mode
        else:
            self.device_id = SelectGpuIdAuto()[0:njobs]
            cuda.get_device(self.device_id[0]).use()
    
    def set_optimizer(self, opt):
#        if ["type"] == "adam":
#            self.optimizer = optimizers.Adam()
#        elif self.specs["learning_rule"]["type"] == "adadelta":
#            self.optimizer = optimizers.AdaDelta()
#        elif self.specs["learning_rule"]["type"] == "momentum":
#            self.optimizer = optimizers.MomentumSGD()
#        elif self.specs["learning_rule"]["type"] == "SGD":
#            self.optimizer = optimizers.SGD()
#        else:
#            raise ValueError("Unsupported rule" + str(self.specs["learning_rule"]["type"]))
        self.optimizer = opt
        self.specs['optimizer'] = opt

    def initialize(self):
        self.layers.to_gpu(self.device_id[0])
        self.optimizer.setup(self.layers)

    def update(self):
        self.optimizer.update()
    def zero_grads(self):
        self.layers.zerograds()

    def forward(self, x, test=False, finetune=False):
        if self.device_id[0] >=0:
            x  = cuda.to_gpu(x, device=self.device_id[0])
        return self.layers(Variable(x), test, finetune)

    def loss_softmax(self, x, y, test=False, finetune=False):
        if self.device_id[0] >=0:
            y = cuda.to_gpu(y, device=self.device_id[0])
        return softmax_cross_entropy(self.forward(x, test,finetune), Variable(y))

    ''' ====== IO functions =========='''
    def __write_hdf5__(self, specs, h, dirname):
        if type(specs) == dict:
            for key in specs:
                if type(specs[key]) is dict:
                    h.create_group(key)
                    self.__write_hdf5__(specs[key], h, dirname+'/'+key)
                elif type(specs[key]) == list:
                    self.__write_hdf5__(specs[key], h, dirname+'/'+key)
                else:
                    if key is not 'W' and key is not 'b':
                        h.create_dataset(dirname+'/'+key, data=specs[key])
        elif type(specs) == list:
            cnt=0
            for l in specs:
                subdirname = '/layer' + str(cnt)
                h.create_group(dirname + subdirname)
                self.__write_hdf5__(l, h, dirname + subdirname)
                cnt+=1
        else:
            h.create_dataset(dirname, data=specs)

    def save(self, filename):
        with h5py.File(filename, 'w') as h:
            self.__write_hdf5__(self.specs['layers_specs'], h, 'layers_specs')
            s=HDF5Serializer(h)
            s.save(self.layers)

    def load(self, filename):
        with h5py.File(filename, 'r') as h:
            layers_specs=[]
            for layer_id in h['layers_specs']:
                specs = {key: h['layers_specs'][layer_id][key].value for key in h['layers_specs'][layer_id]}
                layers_specs.append(specs)
            self.layers = CNN(layers_specs)
            s=HDF5Deserializer(h)
            s.load(self.layers)

    def load_parameters_from_nnet(self, filename):
        '''
        Load parameters from Kaldi nnet format
        '''
        layers_specs=[layer for layer in convert_nnet_to_conf(filename)]
        layers_specs[-1]['activation'] = 'linear'
        self.layers = CNN(layers_specs)
        self.specs['layers_specs'] = layers_specs
        cnt = 0
        for layer in self.specs['layers_specs']:
            print 'Layer'+str(cnt)
            print layer['activation'], layer['dimensions']
            cnt+=1

#-------------------------------------------------------------------
#-------------------------------------------------------------------
#-------------------------------------------------------------------

class CNN(ChainList):
    def __init__(self, specs):
        assert type(specs)==list, \
            "Specs must be a list (Not " + str(type(specs)) + ")\n" +  str(specs)
        self.specs = specs
        self.n_layers = len(specs)
        layers=[]
        for spec in specs:
            if spec["type"] == "conv":
                layers.append(ConvLayer(spec))
            elif spec["type"] == "full":
                layers.append(HiddenLayer(spec))
            elif spec["type"] == "BN":
                assert spec.has_key("dimensions"), "Size of channel dimensions is required"
                layers.append(BatchNormalization(spec["dimensions"]))
            elif spec["type"] == "deconv":
                layers.append(ConvLayer(spec, isDecoding=True))
            else:
                raise ValueError("Unsupported layer" + spec["type"])
        super(CNN, self).__init__(*layers)

    def __call__(self, x, test, upd_batch_est):
        h = self[0](x, test, upd_batch_est)
        for i in xrange(1, self.n_layers):
            h = self[i](h, test, upd_batch_est)
        return h

    def deep_copyparams(self, cnn):
        for i in xrange(0, self.n_layers):
            self[i].copyparams(cnn[i])


class Layer():
    def __apply_activation__(self, h):
        if self.specs.has_key("activation"):
            if self.specs["activation"] == "relu":
                h = F.relu(h)
            elif self.specs["activation"] == "linear":
                h = h
            elif self.specs["activation"] == "Sigmoid":
                h = F.sigmoid(h)
            else:
                raise ValueError("Unsupported activation function: " + self.specs["activation"])
        return h


class ConvLayer(Chain, Layer):
    def __init__(self, specs, isDecoding=False):
        self.specs = specs
        self.is_decoding = isDecoding
        assert self.specs.has_key("filter_shape"), \
            "Please specify filter shape: (out_channels, in_channels, ksize_x, ksize_y)\n" + \
            "Current specs is "+ str(specs)
        out_channels =  self.specs["filter_shape"][0]
        in_channels  =  self.specs["filter_shape"][1]
        ksize        = (self.specs["filter_shape"][2], self.specs["filter_shape"][3])
        if self.is_decoding:
            conv = L.Deconvolution2D(in_channels, out_channels, ksize)
        else:
            conv = L.Convolution2D(in_channels, out_channels, ksize)
        if self.specs.has_key("BN"):
            assert len(self.specs["BN"]) == 2, \
                "argument of BN must be ( {True or False}, {True or False} ). " + \
                "The first element corresponds to the output of activation function " + \
                "and the second element corresponds to the output of pooling function (if it exists)."
            if sum(self.specs["BN"]) == 1:
                super(ConvLayer, self).__init__(
                    conv = conv,
                    bn1  = BatchNormalization(out_channels)
                )
            elif sum(self.specs["BN"]) == 2:
                assert self.specs.has_key("pool_shape"), \
                    "Pooling layer is required if batch normalization is used after pooling function"
                super(ConvLayer, self).__init__(
                    conv = conv,
                    bn1  = BatchNormalization(out_channels),
                    bn2  = BatchNormalization(out_channels)
                )
        else:
            super(ConvLayer, self).__init__(
                conv = conv
            )

    def __call__(self, x, test, finetune):
        if self.is_decoding:
            if self.specs.has_key("pool_shape"):
                # Reshape concatinated vector from the previous hidden layer into image
                if len(x.data.shape) == 2:
                    assert self.specs.has_key("d_out"), \
                        "Please specify output dimension (This will be automaticaly estimated in the future version)"
                    x = F.reshape(x,(x.data.shape[0], self.specs["filter_shape"][1], 
                                     self.specs["d_out"][0], self.specs["d_out"][1]))
                d_out = (x.data.shape[2] * self.specs["pool_shape"][0],
                         x.data.shape[3] * self.specs["pool_shape"][1])
                h = max_unpooling_2d(x, d_out, self.specs["pool_shape"])
            h = self.conv(h)
            h = self.__apply_activation__(h)
        else:
            # So far, batch normalization is only implemented for encoding network
            h = self.conv(x)
            if self.specs.has_key("BN") and self.specs["BN"][0]:
                h = self.bn1(h, test, finetune)
            h = self.__apply_activation__(h)
            if self.specs.has_key("pool_shape"):
                h = F.max_pooling_2d(h, self.specs["pool_shape"])
            if self.specs.has_key("BN") and self.specs["BN"][1]:
                if self.specs["BN"][0]:
                    h = self.bn2(h, test, finetune)
                else:
                    h = self.bn1(h, test, finetune)
        return h
#        return F.max_pooling_2d(F.relu(self.conv(x)), self.specs["pool_shape"])

class HiddenLayer(Chain, Layer):
    def __init__(self, specs):
        self.specs = specs
        assert self.specs.has_key("dimensions"), "Please specify dimensions (input, output)"
        in_dim, out_dim = self.specs["dimensions"]
        initialW     = self.specs['W'] if self.specs.has_key('W') else None
        initial_bias = self.specs['b'][:,0] if self.specs.has_key('b') else None
        if self.specs.has_key("BN") and self.specs["BN"]:
            super(HiddenLayer, self).__init__(
                ln = L.Linear(in_dim, out_dim, initialW=initialW, initial_bias=initial_bias),
                bn = BatchNormalization(out_dim)
            )
        else:
            super(HiddenLayer, self).__init__(
                ln = L.Linear(in_dim, out_dim, initialW=initialW, initial_bias=initial_bias),
            )

    def __call__(self,x, test, finetune):
        h = self.ln(x)
        if self.specs.has_key("BN") and self.specs["BN"]:
            h = self.bn(h, test, finetune)
        h = self.__apply_activation__(h)
        if self.specs.has_key("dropout") :
            assert self.specs["dropout"]>0 and self.specs["dropout"] <=1, \
                "Dropout ratio must be within (0,1]"
            h = F.dropout(h, self.specs["dropout"], train = not test)
        return h

if __name__ == "__main__":
    option_dict={}
    model = NN_parallel(option_dict, njobs=1)
#    model.set_optimizer(optimizers.SGD(0.008))
    model.load_parameters_from_nnet('/data2/tawara/work/ttic/MyPython/src/kaldi/timit/tmp/model1')
#    model.initialize()

    model.save('tmp.h5')
    model.load('tmp.h5')
