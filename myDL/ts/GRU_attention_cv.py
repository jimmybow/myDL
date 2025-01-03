# -*- coding: utf-8 -*-
"""
Created on Mon Nov 12 17:38:23 2018

@author: jimmybow
"""

import torch
import torch.utils.data as Data
from torch import nn
import torch.nn.functional as F
import random
import numpy as np
import pandas as pd
from sklearn import preprocessing
from sklearn.metrics import mean_squared_error
from sklearn.model_selection import GridSearchCV, KFold, TimeSeriesSplit
from mydf import *
from datetime import datetime, timedelta
import math
import copy
import pkg_resources
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.font_manager import FontProperties
from pandas.plotting import register_matplotlib_converters
import io
import imageio

###############################################################################################################
###  define class
###############################################################################################################
class torch_Dataset(Data.Dataset): # 需要继承 data.Dataset
    def __init__(self, x, y):
        self.x = x
        self.y = y
    def __getitem__(self, index):
        data = (self.x[index], self.y[index])
        return data
    def __len__(self):
        return len(self.y)

class AttnDecoderRNN(nn.Module):
    def __init__(self, hidden_size, max_length):
        super(AttnDecoderRNN, self).__init__()
        self.hidden_size = hidden_size
        self.max_length = max_length

        self.attn = nn.Linear(2*self.hidden_size, self.max_length) # 512 => 10
        self.attn_combine = nn.Linear(2*self.hidden_size, self.hidden_size) # 512 => 256
        self.gru = nn.GRU(self.hidden_size, self.hidden_size, batch_first=True)

    def forward(self, input, hidden, encoder_last_layer_outputs):
        attn = self.attn(torch.cat((input[0], hidden[0]), 1)) # (1, 2*hidden_size) => (1, max_length)
        attn_weights = F.softmax(attn, dim=1)                    # (1, max_length)
        # (1, 1, max_length) X (1, max_length, hidden_size) = (1, 1, hidden_size)  encoder_last_layer_outputs 的線性組合
        attn_applied = torch.bmm(attn_weights.unsqueeze(0), encoder_last_layer_outputs.unsqueeze(0))  # (1, 1, hidden_size)

        output = torch.cat((input[0], attn_applied[0]), 1)    # (1, 2*hidden_size)
        output = self.attn_combine(output).unsqueeze(0)          # (1, 1, hidden_size)

        output = F.relu(output)                                  # (1, 1, hidden_size)
        output, hidden = self.gru(output, hidden)
        return output, hidden      # (1, 1, hidden_size), (1, 1, hidden_size)

class GRU_seq2seq(nn.Module):
    def __init__(self, input_size=5, hidden_size=10, output_size=3, prediction_length=12, time_step=24):
        super(GRU_seq2seq, self).__init__()
        
        self.prediction_length = prediction_length
        self.GRU_encoder = nn.GRU(input_size, hidden_size, batch_first=True)
        self.GRU_decoder = AttnDecoderRNN(hidden_size, max_length=time_step)
        self.reg = nn.Linear(hidden_size, output_size) 
      
    def forward(self, x):
        x, encoder_hidden = self.GRU_encoder(x) 
        encoder_last_layer_outputs = x[0]
        decoder_hidden = encoder_hidden[[-1]]
        decoder_input = decoder_hidden  # (1, 1, hidden_size)
        decoder_output_list = []
        for di in range(self.prediction_length):
            decoder_output, decoder_hidden = self.GRU_decoder(decoder_input, decoder_hidden, encoder_last_layer_outputs)
            decoder_output_list.append(decoder_output)
            decoder_input = decoder_output

        output = torch.cat(decoder_output_list, 1)      # (1, prediction_length, hidden_size)
        output = self.reg(output)                       # (1, prediction_length, 3)
        return output

class QuantileLoss(nn.Module):
    def __init__(self, quantiles):
        super().__init__()
        self.quantiles = quantiles
        
    def forward(self, preds, target):
        assert not target.requires_grad
        assert preds.size(0) == target.size(0)
        losses = []
        for i, q in enumerate(self.quantiles):
            errors = target - preds[:, :, i]
            losses.append(
                torch.max( (q-1) * errors, q * errors ).unsqueeze(2)
            )
        result = torch.sum(torch.cat(losses, dim=2), dim=2)
        w = torch.unsqueeze(torch.arange(result.shape[1],0,-1), 1).float()/np.arange(result.shape[1],0,-1).sum()
        loss = torch.mean(torch.mm(result, w))
        return loss
###############################################################################################################
###  define function
###############################################################################################################
def RMSE(x, y, model, std_target, scaler):
    predict_value = [model(x[[i]])[0, :, 0].data.numpy() for i in range(len(x))]
    actual_value =  y.data.numpy()
    return np.sqrt(mean_squared_error(predict_value, actual_value))*std_target*scaler    
    
def fit(data_source, target_column, output_filename = None, model_source = None,
        freq = '1M',
        data_use_length = None,
        prediction_length = 12,
        time_step = 24,
        prediction_quantile = [0.05, 0.95],
        test_size = 0,
        cv_mode = 'kfold',
        n_splits = 5,
        hidden_size = 50,
        learning_rate = 1e-2,
        weight_decay = 0,
        early_stopping_patience = 500,
        epochs = 5000):
    ###############################################################################################################
    ###  preprocessing data
    ###############################################################################################################
    Quantile = [0.5] + prediction_quantile
    mini_batch_size = 1
    
    if isinstance(data_source, str):
        df = pd.read_csv(open(data_source))
    else:
        df = data_source
        
    if 'time' in df.columns.tolist():
        df.index = df.time.astype('datetime64[ns]')
        df >>= drop('time')

    df = df.astype(float).resample(freq).mean()
    if data_use_length is not None: df = df.iloc[-data_use_length:]
    features = df.columns.tolist()
    target_column_index = features.index(target_column)
    time_index = df.index
    if df.isnull().values.sum() > 0:
        print("The data source has missing value after aggregated by freq = '{}'".format(freq))
        print("Filling missing value use method = 'pad'")
        df = df.fillna(method = 'pad').dropna()
    elif len(df) <= time_step + prediction_length - 1:
        raise Exception("The sample size is too low after aggregated, it's not enough to training")
        
    scaler = 10
    scaler_std = preprocessing.StandardScaler()
    data_norm = scaler_std.fit_transform(df)/scaler
    
    # 只會有 len(data_norm) - time_step + 1 個樣本 = len(sample_ranges_x)
    sample_ranges_x = [range(i, i + time_step) for i in range(len(data_norm)) if i + time_step <= len(data_norm)]
    data_x = torch.tensor([data_norm[sample_range] for sample_range in sample_ranges_x]).float()

    # 只會有 len(data_norm) - prediction_length + 1 個樣本 = len(sample_ranges_y)
    sample_ranges_y = [range(i, i + prediction_length) for i in range(len(data_norm)) if i + prediction_length <= len(data_norm)]
    data_y = torch.tensor([data_norm[sample_range, target_column_index] for sample_range in sample_ranges_y]).float()
    
    # 最終只會有 len(sample_ranges_x) - prediction_length = len(sample_ranges_y) - time_step 個學習樣本
    data_x = data_x[:-prediction_length]
    data_y = data_y[time_step:]
    final_ranges_x = sample_ranges_x[:-prediction_length]
    final_ranges_y = sample_ranges_y[time_step:]
    ###############################################################################################################
    ###  apply window-move size = prediction_length/3
    ###############################################################################################################
    #window_move = math.ceil(prediction_length/5)
    #data_x = data_x[::window_move]
    #data_y = data_y[::window_move]
    ###############################################################################################################
    ###  k-fold split
    ###############################################################################################################
    if test_size > 0:
        test_x = data_x[-test_size:]
        test_y = data_y[-test_size:] 
        data_x = data_x[:-test_size]    
        data_y = data_y[:-test_size]

    if cv_mode == 'kfold':
        splits = list(KFold(n_splits=n_splits, shuffle=True, random_state=2019).split(data_x, data_y))
    else:
        splits = list(TimeSeriesSplit(n_splits=n_splits).split(data_x, data_y))
    train_size = len(splits[0][0])
    validate_size = len(splits[0][1])
    ###############################################################################################################
    ###  model train
    ###############################################################################################################
    loss_func = QuantileLoss(Quantile)
    if model_source is not None:
        if isinstance(model_source, str): 
            model_list = torch.load(model_source)['model_list'] 
        else: 
            model_list = model_source['model_list']
    else:
        model_list = []

    for model_index, (train_idx, validate_idx) in enumerate(splits):
        print("Beginning fold {}".format(model_index))
        train_dataset = torch_Dataset(data_x[train_idx], data_y[train_idx])
        train_loader = Data.DataLoader(dataset = train_dataset, batch_size = mini_batch_size, shuffle = True)
        validate_x = data_x[validate_idx]
        validate_y = data_y[validate_idx]

        net = GRU_seq2seq(input_size = len(features),
                          output_size = len(Quantile),
                          hidden_size = hidden_size,
                          time_step = time_step,
                          prediction_length = prediction_length)

        if model_source is not None:
            net.load_state_dict(model_list[model_index]['state_dict'])
        optimizer = torch.optim.Adam(net.parameters(), lr = learning_rate, weight_decay = weight_decay)
        
        std_target =  np.sqrt(scaler_std.var_)[target_column_index]
        if std_target == 0: std_target = 1
    
        train_loss_list = []
        validate_loss_list = []
        for epoch in range(epochs):
            # training mode
            net.train()     
            for step, (x, y) in enumerate(train_loader, 1): 
                # 前向传播
                out = net(x)  # (mini_batch, 12, 3)
                loss = loss_func(out, y)  
                # 反向传播
                optimizer.zero_grad()
                loss.backward()
                optimizer.step()
            # evaluation mode
            net.eval()
            with torch.no_grad():
                #validate_loss = loss_func(net(validate_x), validate_y).item()
                validate_loss = RMSE(validate_x, validate_y, net, std_target, scaler)
            validate_loss_list.append(validate_loss)
            train_loss_list.append(loss.item())
            if epoch == 0 or validate_loss < best_validate_loss:            
                best_validate_loss = validate_loss
                best_state_dict = copy.deepcopy(net.state_dict())
                best_epoch = epoch
                print('Best Epoch:', epoch, 'Train_loss:', '%.10f' % loss.item(), 'Validate_loss:', '%.10f' % validate_loss)
            elif epoch - best_epoch > early_stopping_patience: 
                print("Validate_RMSE don't imporved for {} epoch, training stop !".format(early_stopping_patience))
                break
            else:
                print('---- Epoch:', epoch, 'Train_loss:', '%.10f' % loss.item(), 'Validate_loss:', '%.10f' % validate_loss)

        if model_source is None:
            model_list.append({
                'state_dict': best_state_dict,
                'best_epoch': best_epoch,
                'best_validate_loss': best_validate_loss,
                'train_loss_list': train_loss_list,
                'validate_loss_list': validate_loss_list
            })
    ######################################################################################################
    ###  final model evaluation
    ######################################################################################################
    RMSE_list = []
    for model_index in range(len(model_list)):
        best_validate_loss = model_list[model_index]['best_validate_loss']
        print('model {}:'.format(model_index))
        print('-- Best validate loss = {}'.format(best_validate_loss))
        RMSE_list.append(best_validate_loss)

    mean_CV_RMSE = np.mean(RMSE_list) 
    print('mean CV RMSE =', mean_CV_RMSE)

    if test_size > 0:
        pred_result_total = []
        for i in range(test_size):
            pred_result_list = []
            for inner_model in model_list:
                net.load_state_dict(inner_model['state_dict'])
                net.eval()
                with torch.no_grad():
                    pred_result_list.append( net(test_x[[i]])[0, :, 0].data.numpy() )
            pred_result = np.mean(pred_result_list, axis=0)
            pred_result_total.append(pred_result)
        test_RMSE = np.sqrt(mean_squared_error(pred_result_total, test_y.data.numpy()))*std_target*scaler  
    else:
        test_RMSE = -1
    ###############################################################################################################
    ###  Output 
    ###############################################################################################################
    output_model = {
        'model_list': model_list,
        'mean_CV_RMSE' : mean_CV_RMSE,
        'scaler_std':scaler_std,
        'features': features,
        'target_column':target_column,
        'freq': freq,
        'hidden_size': hidden_size,
        'Quantile': Quantile,
        'mini_batch_size': mini_batch_size,
        'learning_rate': learning_rate,
        'early_stopping_patience': early_stopping_patience,
        'epochs': epochs,
        'time_step': time_step,
        'prediction_length': prediction_length,
        'train_size':train_size,
        'validate_size':validate_size,
        'test_size':test_size,
        'n_splits':n_splits,
        'cv_mode':cv_mode,
        'weight_decay':weight_decay,
        'test_RMSE': test_RMSE
    }
    
    if output_filename is not None: torch.save(output_model, output_filename)
    return output_model
    ###############################################################################################################
    ###  End
    ###############################################################################################################

def predict(data_source, model_source, predict_start_time):
    if isinstance(model_source, str): 
        model = torch.load(model_source)
    else: 
        model = model_source
    
    if isinstance(data_source, str):
        df = pd.read_csv(open(data_source))
    else:
        df = data_source
    
    model_list = model['model_list']
    features = model['features']
    target_column = model['target_column']
    freq = model['freq']
    time_step = model['time_step']
    prediction_length = model['prediction_length']
    hidden_size = model['hidden_size']
    Quantile = model['Quantile']
    scaler_std = model['scaler_std']
    scaler = 10
    
    if 'time' in df.columns.tolist():
        df.index = df.time.astype('datetime64[ns]')
        df >>= drop('time')
       
    df = df[features].astype(float).resample(freq).mean()
    time_index = pd.date_range(df.index[0], periods= len(df) + prediction_length , freq = freq)
    target_column_index = features.index(target_column)
    if df.isnull().values.sum() > 0:
        print("The data source has missing value after aggregated by freq = '{}'".format(freq))
        print("Filling missing value use method = 'pad'")
        df = df.fillna(method = 'pad').dropna()
    
    predict_range = where(time_index >= predict_start_time)[:prediction_length]
    if len(predict_range) < prediction_length:  predict_range = np.arange(len(time_index))[-prediction_length:]
    
    input_data_range = np.arange(predict_range[0] - time_step, predict_range[0])
    if sum(input_data_range < 0) > 0 :
        input_data_range = np.arange(time_step)
        predict_range = np.arange(time_step, time_step + prediction_length)
    
    net = GRU_seq2seq(input_size = len(features),
                      output_size = len(Quantile),
                      hidden_size = hidden_size,
                      time_step = time_step,
                      prediction_length = prediction_length)
    
    std_target =  np.sqrt(scaler_std.var_)[target_column_index]
    if std_target == 0: std_target = 1
    mean_target =  scaler_std.mean_[target_column_index]

    pred_result_list = []
    inpu_data = torch.tensor(scaler_std.transform(df.iloc[input_data_range])).float().unsqueeze(0)/scaler
    for inner_model in model_list:
        net.load_state_dict(inner_model['state_dict'])
        net.eval()
        with torch.no_grad():
            pred_result_list.append( net(inpu_data).data.numpy()[0]*std_target*scaler + mean_target )

    pred_result = np.mean(pred_result_list, axis=0)
    pred_result_dict = {str(q):pred_result[:, i].tolist() for i, q in enumerate(Quantile)}
    return {'predict_target': target_column,
            'predict_result': pred_result_dict,
            'time': time_index[predict_range].strftime('%Y-%m-%d %H:%M:%S').tolist()}

def predict_to_gif(data_source, model_source, predict_start_time, filename,
                   ticks_step = 15, size = [15, 7]):
    if isinstance(model_source, str): 
        model = torch.load(model_source)
    else: 
        model = model_source
    
    if isinstance(data_source, str):
        df = pd.read_csv(open(data_source))
    else:
        df = data_source
    
    features = model['features']
    target_column = model['target_column']
    freq = model['freq']
    time_step = model['time_step']
    prediction_length = model['prediction_length']
    Quantile = model['Quantile']
    n_splits = model['n_splits']
    mean_CV_RMSE = model['mean_CV_RMSE']
    test_RMSE = model['test_RMSE']
    cv_mode = model['cv_mode']

    if 'time' in df.columns.tolist():
        df.index = df.time.astype('datetime64[ns]')
        df >>= drop('time')
       
    df = df[features].astype(float).resample(freq).mean()
    time_index = pd.date_range(df.index[0], periods= len(df) + prediction_length , freq = freq)
    target_column_index = features.index(target_column)
    if df.isnull().values.sum() > 0:
        print("The data source has missing value after aggregated by freq = '{}'".format(freq))
        print("Filling missing value use method = 'pad'")
        df = df.fillna(method = 'pad').dropna()

    fname = pkg_resources.resource_filename(__name__, '../Fonts/kaiu.ttf')
    image_list = []
    register_matplotlib_converters()
    fig, ax = plt.subplots()
    for i in predict_start_time:
        pred_result = predict(data_source, model_source, i)
        time_index = pd.date_range(df.index[0], periods = len(df) + len(pred_result['time']), freq = freq)
        time_index_pred = pd.date_range(pred_result['time'][0], periods = len(pred_result['time']), freq = freq)
        diff = (time_index[1] - time_index[0]).total_seconds()
        if diff >= 360*86400:
            time_index_label = time_index.astype(str).str.slice(0, 4)
            title = '{} 未來走勢預測 (預測開始於 {})'.format(target_column, pred_result['time'][0][:4])        
        elif diff >= 20*86400:
            time_index_label = time_index.astype(str).str.slice(0, 7)
            title = '{} 未來走勢預測 (預測開始於 {})'.format(target_column, pred_result['time'][0][:7])
        elif diff >= 86400:
            time_index_label = time_index.astype(str).str.slice(0, 10)
            title = '{} 未來走勢預測 (預測開始於 {})'.format(target_column, pred_result['time'][0][:10])
        elif diff >= 3600:
            time_index_label = time_index.astype(str).str.slice(0, 13)
            title = '{} 未來走勢預測 (預測開始於 {})'.format(target_column, pred_result['time'][0][:13])
        elif diff >= 60:
            time_index_label = time_index.astype(str).str.slice(0, 16)
            title = '{} 未來走勢預測 (預測開始於 {})'.format(target_column, pred_result['time'][0][:16])    
        else:
            time_index_label = time_index.astype(str)
            title = '{} 未來走勢預測 (預測開始於 {})'.format(target_column, pred_result['time'][0])

        font = FontProperties(fname = fname, size = 15)
        fig.set_size_inches(*size)
        p1 = ax.plot(df[target_column], 'b')[0]
        p2 = ax.plot(time_index_pred, pred_result['predict_result'][str(Quantile[0])], 'r')[0]
        p3 = ax.fill_between(time_index_pred,  pred_result['predict_result'][str(Quantile[1])],  pred_result['predict_result'][str(Quantile[2])], color = 'c')
        p4 = ax.plot([], [], ' ')[0]
        if cv_mode == 'kfold':
            label_cv_mode = 'fold'
        else:
            label_cv_mode = 'ts'

        ax.legend([p1, p2, p3, p4, p4], 
                ('實際值', '預測值', '{:.0%} 預測區間'.format(Quantile[2]-Quantile[1]), 
                '{}-{} CV mean RMSE = {:.4f}'.format(n_splits, label_cv_mode, mean_CV_RMSE),
                'test RMSE = {:.4f}'.format(test_RMSE)    )
                , loc='best', prop=font)
        ax.set_xticks(time_index[::ticks_step])
        ax.set_xticklabels(time_index_label[::ticks_step])
        ax.set_ylabel(target_column, fontproperties=font, fontsize = 20)
        ax.set_xlabel('時間', fontproperties=font, fontsize = 20) 
        ax.set_title(title, fontproperties=font, fontsize = 30, y = 1.03)
        buf = io.BytesIO()            
        fig.savefig(buf)
        plt.cla()
        buf.seek(0)
        image_list.append(imageio.imread(buf))
    imageio.mimsave(filename, image_list, fps=1, loop=0)
