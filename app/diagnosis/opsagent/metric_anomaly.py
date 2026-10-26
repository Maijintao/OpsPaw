import numpy as np
from sklearn.metrics import precision_recall_fscore_support
from sklearn.model_selection import train_test_split
import torch
import torch.nn as nn
from tqdm import tqdm
from torch.utils.data import Dataset, DataLoader
import matplotlib.pyplot as plt
import time
import sys

import logging
import sys
import os
import pandas as pd

import warnings
import math
import numpy as np
import pytz
import csv
from datetime import datetime

def get_logger(name):
    is_debug = True if sys.gettrace() else False
    logger=logging.getLogger(name)


    if is_debug:
        log_level=logging.DEBUG
    else:
        log_level=logging.INFO
    logger.propagate = False

    if not logger.hasHandlers():
        console = logging.StreamHandler()
        formatter=logging.Formatter("%(asctime)s - %(levelname)s - %(funcName)s - %(message)s")
        console.setFormatter(formatter)
        console.setLevel(log_level)
        logger.addHandler(console)
        logger.setLevel(log_level)
    return logger

BATCH_SIZE = 32
INIT_LR = 1e-4
logger = get_logger("classification")

def min_max_normalization(x: np.ndarray):
    if not isinstance(x, np.ndarray):
        logger.error("min_max_normalization: input must be np.ndarray")
    if x.ndim == 1:
        return (x-x.min())/(x.max()-x.min())
    else:
        x = x.T
        x = (x-x.min(axis=0))/(x.max(axis=0)-x.min(axis=0))
        return x.T

class DataSetCNN(Dataset):
    def __init__(self, data, label, length=30, eval=False):
        if data.ndim == 1:
            data = data.reshape(1, -1)
        if not eval:
            assert(data.shape[0] == label.shape[0])
            self.datas = data
            self.label = label
        else:
            self.datas = data
        self.length = length
        self.eval = eval

    def __len__(self):
        return self.datas.shape[0]

    def __getitem__(self, i):
        if self.eval:
            return torch.tensor(self.datas[i].reshape(-1, self.length), dtype=torch.float)
        else:
            return torch.tensor(self.datas[i].reshape(-1, self.length), dtype=torch.float), torch.tensor(self.label[i], dtype=torch.long)

class CNN(nn.Module):
    def __init__(self, classes=50):
        super(CNN, self).__init__()
        self.conv1 = nn.Sequential(
            nn.Conv1d(1, 64, 5, 1, 1),
            # nn.BatchNorm1d(64),
            nn.ReLU(),
            nn.MaxPool1d(kernel_size=2),
            # nn.AdaptiveAvgPool1d(1)
        )
        self.conv2 = nn.Sequential(
            nn.Conv1d(64, 128, 5, 1, 1, bias=False),
            # nn.BatchNorm1d(128),
            nn.ReLU(),
            nn.MaxPool1d(2),
        )
        self.conv3 = nn.Sequential(
            nn.Conv1d(128, 256, 5, 1, 1, bias=False),
            # nn.BatchNorm1d(256),
            nn.ReLU(),
            nn.MaxPool1d(2),
        )
        self.flatten = nn.Sequential(
            nn.Linear(512, 64), nn.BatchNorm1d(64), nn.ReLU())
        # self.out = nn.Linear(256, 64)
        self.out = nn.Linear(64, classes)
        self.dropout = nn.Dropout(0.5)
        self.avgpool = nn.AdaptiveAvgPool1d(1)

    def forward(self, x):
        # print(x)
        x = self.conv1(x)

        x = self.conv2(x)

        x = self.conv3(x)

        x = x.view(x.size(0), -1)
        x = self.dropout(x)
        x = self.flatten(x)
        # x=torch.cat((x,y),1)
        # x=self.avgpool(x)
        x = self.out(x)
        # x=self.out1(x)
        x = nn.LogSoftmax(dim=-1)(x)
        # x=nn.Softmax(dim=-1)(x)
        return x


class CNNClassifier(object):
    def __init__(self, model=None, class_num=11, length=30):
        if model is None:
            self.model = CNN(class_num)
        else:
            self.model = model
        self.class_num = class_num
        self.length = length

    def fit(self, X: np.ndarray, y: np.array, max_epoch=100, stop_epoch=10):
        logger.info("start training")
        # ------init--------
        initial_lr = INIT_LR
        batch_size = BATCH_SIZE
        criterion = nn.CrossEntropyLoss()
        count = 0
        train_X, valid_X, train_y, valid_y = train_test_split(
            X, y, test_size=0.3)
        train_data, valid_data = DataSetCNN(train_X, train_y, length=self.length), DataSetCNN(
            valid_X, valid_y, length=self.length)
        optimizer = torch.optim.Adam(self.model.parameters(), lr=initial_lr)
        train_loader = DataLoader(
            train_data, batch_size=batch_size, shuffle=True, drop_last=True)
        valid_loader = DataLoader(
            valid_data, batch_size=batch_size, shuffle=True, drop_last=True)
        is_gpu = torch.cuda.is_available()
        if is_gpu:
            self.model = self.model.cuda()
            criterion = criterion.cuda()
        scheduler = torch.optim.lr_scheduler.StepLR(
            optimizer, step_size=50, gamma=0.8)
        max_f, best_p, best_r = 0, 0, 0
        best_model = None

        # ------train------
        for epoch in range(max_epoch):
            y_true, y_pred = np.array(
                [], dtype=np.int32), np.array([], dtype=np.int32)
            self.model.train()
            # for b in tqdm(train_loader):
            for _, b in enumerate(train_loader):
                optimizer.zero_grad()
                if is_gpu:
                    b[0] = b[0].cuda()
                    b[1] = b[1].cuda()
                out = self.model(b[0])
                batch_pred = out.data.max(1)[1]
                loss = criterion(out, b[1])
                loss.backward()
                optimizer.step()
                if is_gpu:
                    b[1] = b[1].cpu()
                    batch_pred = batch_pred.cpu()
                y_true = np.append(y_true, b[1].numpy())
                y_pred = np.append(y_pred, batch_pred.numpy())
            p1, r1, f1, _ = precision_recall_fscore_support(
                y_true, y_pred, labels=np.unique(y_pred), average="macro")
            scheduler.step()

        # ----------validation-------------
            with torch.no_grad():
                self.model.eval()
                y_true, y_pred = np.array(
                    [], dtype=np.int32), np.array([], dtype=np.int32)
                for _, b in enumerate(valid_loader):
                    if is_gpu:
                        b[0] = b[0].cuda()
                        b[1] = b[1].cuda()
                    out = self.model(b[0])
                    batch_pred = out.data.max(1)[1]
                    if is_gpu:
                        b[1] = b[1].cpu()
                        batch_pred = batch_pred.cpu()
                    y_true = np.append(y_true, b[1].numpy())
                    y_pred = np.append(y_pred, batch_pred.numpy())
                p2, r2, f2, _ = precision_recall_fscore_support(
                    y_true, y_pred, labels=np.unique(y_pred), average="macro")
                if f2 > max_f:
                    best_p = p2
                    best_r = r2
                    max_f = f2
                    best_model = self.model
                    count = 0
                else:
                    count += 1
                    if count >= stop_epoch:
                        break
            logger.info("Epoch:{}, Loss:{:.5f}\ntrain_p:{:.5f}, train_r:{:.5f}, train_f:{:.5f},valid_p:{:.5f}, valid_r:{:.5f}, valid_f:{:.5f},best_p:{:.5f}, best_r:{:.5f}, best_f:{:.5f}".format(
                epoch+1, loss.item(), p1, r1, f1, p2, r2, f2, best_p, best_r, max_f))
        self.model = best_model

    def predict(self, X: np.ndarray) -> np.array:
        if self.model is None:
            raise ValueError("please load or fit model first")

        test_data = DataSetCNN(min_max_normalization(
            X), None, eval=True, length=self.length)
        test_loader = DataLoader(test_data, batch_size=BATCH_SIZE)
        self.model.eval()
        pred = np.array([], dtype=np.int32)
        for _, b in enumerate(test_loader):
            out = self.model(b)
            batch_pred = out.data.max(1)[1]
            pred = np.append(pred, batch_pred.numpy())
        return pred

    def save_model(self, path):
        if ".pt" not in path:
            path += ".pt"
        torch.save(self.model.state_dict(), path)
        logger.info("save model successfully")

    def load_model(self, path):
        if ".pt" not in path:
            path += ".pt"
        self.model.load_state_dict(torch.load(path))
        logger.info("load model successfully")


def generate_metric_describe(clf,path,services):
    an_type=['Level shift up','Level shift down','Steady increase','Steady decrease','Single spike','Single dip','Transient level shift up','Transient level shift down','Multiple spikes','Multiple dips','Fluctuations']
    anomaly1=[]
    listt = os.listdir(path)
    for name in listt:
        if '.ipynb_' in name:
            continue
        f=0
        for root in services[:5]:
            if root in name:
                f=1
        if f==0:
            continue
        ps = os.path.join(path,name)
        files = os.listdir(ps)
        for f in files:
            if '.ipynb_' in f:
                continue
            try:
                df =  pd.read_csv(os.path.join(ps,f))
                parts = f[:-4].split('_')
                ms = parts[-1].split('-')
                ymean = float(ms[0])
                ystd = float(ms[1])
                i = [int(df.loc[0, 'timestamp']),int(df.loc[29, 'timestamp'])]
                kpi = '_'.join(parts[:-1])
                an='The '+kpi+' metric for the service '+name+' is abnormal,'

                tem = df['value'].tolist()
                with warnings.catch_warnings(record=True) as w:
                  warnings.simplefilter("always")
                  if len(tem)!=30:
                    continue
                  p=clf.predict(np.array(tem))
                  if p[0]%2==0:
                    trend='increase'
                    value=max(tem)
                  else:
                    trend='decrease'
                    value=min(tem)
                  if ystd==0:
                      ystd=0.000001
                  score = round(abs((value-ymean)/ystd)/2,2)

                  if math.isnan(score):
                    continue

                  if score<=1:
                    continue


                  if not w:
                    start_utc = datetime.fromtimestamp(int(i[0]/1000), tz=pytz.UTC)
                    start_local = start_utc.astimezone(pytz.timezone('Asia/Shanghai'))
                    start = start_local.strftime('%Y-%m-%d %H:%M:%S')

                    end_utc = datetime.fromtimestamp(int(i[1]/1000), tz=pytz.UTC)
                    end_local = end_utc.astimezone(pytz.timezone('Asia/Shanghai'))
                    end = end_local.strftime('%Y-%m-%d %H:%M:%S')

                    an+='with anomaly pattern of '+an_type[p[0]]+',started at '+start+',ended at '+end+',reach '+str(value)+\
                    ','+trend +' from the previous '+str(tem[0])+',anomaly score is '+str(score)+'.'
                    anomaly1.append(an)
            except Exception as e:
                continue


    return anomaly1

classifier = CNNClassifier()
classifier.load_model("patterncla.pt")

data = pd.read_csv("pattern/10.csv").iloc[:,0].values

plt.plot(data, marker='o')
plt.title("1D ndarray 折线图")
plt.xlabel("索引位置")
plt.ylabel("数值")
plt.savefig("output_plot.png")

answer = classifier.predict(data)
print(answer)
