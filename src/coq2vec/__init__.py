from typing import List, TypeVar, Dict, Optional, Union, overload, cast, Set
import re
import sys
import contextlib
import pickle
import itertools
import time
from pathlib import Path

import torch
from torch import optim
from torch import nn
import torch.nn.modules.loss as loss
import torch.utils.data as data
from tqdm import tqdm

class DummyFile:
    def write(self, x): pass
    def flush(self): pass

@contextlib.contextmanager
def silent():
    save_stderr = sys.stderr
    save_stdout = sys.stdout
    sys.stderr = DummyFile()
    sys.stdout = DummyFile()
    try:
        yield
    finally:
        sys.stderr = save_stderr
        sys.stdout = save_stdout

with silent():
    use_cuda = torch.cuda.is_available()
cuda_device = "cuda:0"
EOS_token = 1
SOS_token = 0

class CoqRNNVectorizer:
    symbol_mapping: Optional[Dict[str, int]]
    model: Optional['EncoderRNN']
    _decoder: Optional['DecoderRNN']
    def __init__(self) -> None:
        self.symbol_mapping = None
        self.model = None
        self._decoder = None
        pass
        self.symbol_mapping, self.model, self._decoder = torch.load(model_path)
    def load_weights(self, model_path: Union[Path, str]) -> None:
        if isinstance(model_path, str):
            model_path = Path(model_path)
    def save_weights(self, model_path: Union[Path, str]):
        if isinstance(model_path, str):
            model_path = Path(model_path)
        with model_path.open('wb') as f:
            torch.save((self.symbol_mapping, self.model, self._decoder), f)
        pass
    def train(self, terms: List[str],
              hidden_size: int, learning_rate: float, n_epochs: int,
              batch_size: int, print_every: int,
              force_max_length: Optional[int] = None) -> None:
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        token_set: Set[str] = set()
        max_length_so_far = 0
        for term in tqdm(terms, desc="Getting symbols"):
            for symbol in get_symbols(term):
                token_set.add(symbol)
            max_length_so_far = max(len(get_symbols(term)), max_length_so_far)

        token_vocab = list(token_set)
        self.symbol_mapping = {}
        for idx, symbol in enumerate(token_vocab):
            self.symbol_mapping[symbol] = idx
        if force_max_length:
            max_term_length = min(force_max_length, max_length_so_far)
        else:
            max_term_length = max_length_so_far

        term_tensors = maybe_cuda(torch.LongTensor([
            normalize_sentence_length([self.symbol_mapping[symb]
                                       for symb in get_symbols(term)
                                       if symb in self.symbol_mapping],
                                      max_term_length,
                                      EOS_token) + [EOS_token]
            for term in tqdm(terms, desc="Tokenizing and normalizing")])

        data_batches = data.DataLoader(data.TensorDataset(term_tensors),
                                       batch_size=batch_size, num_workers=0,
                                       shuffle=True, pin_memory=True, drop_last=True)
        num_batches = int(term_tensors[0].size()[0] / batch_size)
        dataset_size = num_batches * batch_size

        encoder = maybe_cuda(EncoderRNN(len(token_vocab), hidden_size).to(self.device))
        decoder = maybe_cuda(DecoderRNN(hidden_size, len(token_vocab)).to(self.device))

        optimizer = optim.SGD(itertools.chain(encoder.parameters(), decoder.parameters()),
                              lr=learning_rate)
        criterion = nn.NLLLoss()
        training_start=time.time()
        for epoch in range(n_epochs):
            print("Epoch {} (learning rate {:.6f})".format(epoch, optimizer.param_groups[0]['lr']))
            epoch_loss = 0.
            for batch_num, data_batch in enumerate(data_batches, start=1):
                optimizer.zero_grad()
                loss = autoencoderBatchLoss(encoder, decoder, data_batch, criterion)
                loss.backward()
                optimizer.step()
                epoch_loss += loss.item()
                if batch_num % print_every == 0:
                    items_processed = batch_num * batch_size + \
                      epoch * dataset_size
                    progress = items_processed / \
                      (dataset_size * n_epochs)
                    print("{} ({:7} {:5.2f}%) {:.4f}"
                          .format(timeSince(training_start, progress),
                                  items_processed, progress * 100,
                                  epoch_loss / batch_num))
            self.model = encoder
            self._decoder = decoder
            pass
        pass
    def term_to_vector(self, term_text: str):
        assert self.symbol_mapping
        assert self.model
        term_sentence = get_symbols(term_text)
        term_tensor = maybe_cuda(torch.LongTensor(
            [self.symbol_mapping[symb] for symb in term_sentence
             if symb in self.symbol_mapping] + [EOS_token])).view(-1, 1)
        input_length = term_tensor.size(0)
        with torch.no_grad():
            device = "cuda" if use_cuda else "cpu"
            hidden = self.model.initHidden(device)
            cell = self.model.initCell(device)
            for ei in range(input_length):
                _, hidden, cell = self.model(term_tensor[ei], hidden, cell)
        return hidden.cpu().detach().numpy().flatten()

class EncoderRNN(nn.Module):
    def __init__(self, input_size: int, hidden_size: int) -> None:
        super(EncoderRNN, self).__init__()
        self.hidden_size = hidden_size

        self.embedding = nn.Embedding(input_size, hidden_size)
        self.lstm = nn.LSTM(hidden_size, hidden_size)

    def forward(self, input: torch.LongTensor, hidden: torch.FloatTensor,
                cell: torch.FloatTensor):
        embedded = self.embedding(input).view(1, 1, -1)
        output = embedded
        output, (hidden, cell) = self.lstm(output, (hidden,cell))
        return output, hidden, cell

    def initHidden(self,device):
        return torch.zeros(1, 1, self.hidden_size, device=device)

    def initCell(self,device):
        return torch.zeros(1, 1, self.hidden_size, device=device)

class DecoderRNN(nn.Module):
    def __init__(self, hidden_size: int, output_size: int) -> None:
        super(DecoderRNN, self).__init__()
        self.hidden_size = hidden_size
        self.embedding = nn.Embedding(output_size, hidden_size)
        self.lstm = nn.LSTM(hidden_size, hidden_size)
        self.out = nn.Linear(hidden_size, output_size)
        self.softmax = nn.LogSoftmax(dim=1)

    def forward(self, input, hidden, cell):
        embedded = self.embedding(input).view(1, 1, -1)
        output, (hidden, cell) = self.lstm(F.relu(embedded), (hidden, cell))
        token_dist = self.softmax(self.out(output[0]))
        return token_dist, hidden, cell

    def initHidden(self,device):
        return torch.zeros(1, 1, self.hidden_size, device=device)

    def initCell(self,device):
        return torch.zeros(1, 1, self.hidden_size, device=device)

def autoencoderBatchLoss(encoder: EncoderRNN, decoder: DecoderRNN, data: torch.LongTensor, criterion: loss._Loss) -> torch.FloatTensor:
    input_length = data.size(0)
    target_length = input_length
    device = "cuda" if use_cuda else "cpu"
    encoder_hidden = encoder.initHidden(device)
    encoder_cell = encoder.initCell(device)
    decoder_cell = decoder.initCell(device)

    loss: torch.FloatTensor = torch.FloatTensor([0.])
    for ei in range(input_length):
        encoder_output,encoder_hidden, encoder_cell = encoder(data[ei], encoder_hidden, encoder_cell)

    decoder_input = torch.tensor([[SOS_token]], device=device)
    decoder_hidden = encoder_hidden

    for di in range(target_length):
        decoder_output, decoder_hidden, decoder_cell = decoder(decoder_input, decoder_hidden, decoder_cell)
        topv, topi = decoder_output.topk(1)
        decoder_input = topi.squeeze().detach()

        loss = cast(torch.FloatTensor, loss + cast(torch.FloatTensor, criterion(decoder_output, data[di])))
        if decoder_input.item() == EOS_token:
            break

    return loss


symbols_regexp = (r',|(?::>)|(?::(?!=))|(?::=)|\)|\(|;|@\{|~|\+{1,2}|\*{1,2}|&&|\|\||'
                  r'(?<!\\)/(?!\\)|/\\|\\/|(?<![<*+-/|&])=(?!>)|%|(?<!<)-(?!>)|'
                  r'<-|->|<=|>=|<>|\^|\[|\]|(?<!\|)\}|\{(?!\|)')
def get_symbols(string: str) -> List[str]:
    return [word for word in re.sub(
        r'(' + symbols_regexp + ')',
        r' \1 ', string).split()
            if word.strip() != '']

T = TypeVar('T', bound=nn.Module)
@overload
def maybe_cuda(component: T) -> T:
    ...

@overload
def maybe_cuda(component: torch.Tensor) -> torch.Tensor:
    ...

def maybe_cuda(component):
    if use_cuda:
        return component.to(device=torch.device(cuda_device))
    else:
        return component

def normalize_sentence_length(sentence: List[int], target_length: int, fill_value: int) -> List[int]:
    if len(sentence) > target_length:
        return sentence[:target_length]
    elif len(sentence) < target_length:
        return sentence + [fill_value] * (target_length - len(sentence))
    else:
        return sentence

def timeSince(since : float, percent : float) -> str:
    now = time.time()
    s = now - since
    es = s / percent
    rs = es - s
    return "{} (- {})".format(asMinutes(s), asMinutes(rs))

def asMinutes(s : float) -> str:
    m = int(s / 60)
    s -= m * 60
    return "{:3}m {:5.2f}s".format(m, s)
