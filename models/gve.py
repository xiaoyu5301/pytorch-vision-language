import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.nn.utils.rnn import pack_padded_sequence
from torch.nn.utils.rnn import pad_packed_sequence
from torch.distributions import Categorical

from utils.misc import to_var

class GVE(nn.Module):
    def __init__(self, input_size, word_embed_size, hidden_size,
                 vocab_size, sentence_classifier, num_classes, dropout_prob=0.5):
        super().__init__()

        self.sentence_classifier = sentence_classifier

        self.word_embed = nn.Embedding(vocab_size, word_embed_size, padding_idx=0)

        lstm1_input_size = word_embed_size
        lstm2_input_size = 2*hidden_size + num_classes

        self.linear1 = nn.Linear(input_size, hidden_size)
        self.lstm1 = nn.LSTM(lstm1_input_size, hidden_size, batch_first=True)
        self.lstm2 = nn.LSTM(lstm2_input_size, hidden_size, batch_first=True)
        self.linear2 = nn.Linear(hidden_size, vocab_size)
        self.init_weights()

        self.num_classes = num_classes
        self.input_size = (input_size, vocab_size)
        self.output_size = vocab_size
        self.dropout_prob=dropout_prob


    def init_weights(self):
        self.word_embed.weight.data.uniform_(-0.1, 0.1)
        self.linear1.weight.data.uniform_(-0.1, 0.1)
        self.linear1.bias.data.fill_(0)
        self.linear2.weight.data.uniform_(-0.1, 0.1)
        self.linear2.bias.data.fill_(0)


    def forward(self, image_features, captions, lengths, labels,
            labels_onehot=None):
        if labels_onehot is None:
            labels_onehot = self.convert_onehot(labels)

        embeddings = self.word_embed(captions)
        embeddings = F.dropout(embeddings, p=self.dropout_prob, training=self.training)

        image_features = self.linear1(image_features)
        image_features = F.relu(image_features)
        image_features = F.dropout(image_features, p=self.dropout_prob, training=self.training)
        image_features = torch.cat((image_features, labels_onehot), 1)
        image_features = image_features.unsqueeze(1)
        image_features = image_features.expand(-1, embeddings.size(1), -1)


        packed = pack_padded_sequence(embeddings, lengths, batch_first=True)
        hiddens, _ = self.lstm1(packed)
        unpacked_hiddens, new_lengths = pad_packed_sequence(hiddens, batch_first=True)
        unpacked_hiddens = torch.cat((image_features, unpacked_hiddens), 2)
        unpacked_hiddens = F.dropout(unpacked_hiddens, p=self.dropout_prob, training=self.training)
        packed_hiddens = pack_padded_sequence(unpacked_hiddens, lengths,
                batch_first=True)
        hiddens, _ = self.lstm2(packed_hiddens)


        hiddens = F.dropout(hiddens[0], p=self.dropout_prob, training=self.training)
        outputs = self.linear2(hiddens)
        return outputs

    """
    def state_dict(self):
        state_dict = super(GVE, self).state_dict()
        for key in self.vision_model.state_dict().keys():
            del state_dict['vision_model.{}'.format(key)]
        return state_dict
    """

    def log_sum_exp(self, tensor, keepdim=True):
        r"""
        Numerically stable implementation for the `LogSumExp` operation. The
        summing is done along the last dimension.
        Args:
            tensor (torch.Tensor or torch.autograd.Variable)
            keepdim (Boolean): Whether to retain the last dimension on summing.
        """
        max_val = tensor.max(dim=-1, keepdim=True)[0]
        return max_val + (tensor - max_val).exp().sum(dim=-1, keepdim=keepdim).log()


    def sample(self, logits):
        logits = logits - self.log_sum_exp(logits)
        probs = F.softmax(logits, dim=1)
        dist = Categorical(probs)
        sample = dist.sample()
        return sample, dist.log_prob(sample)

    def generate_sentence(self, image_features, start_word, end_word,
            labels, labels_onehot=None, states=(None,None), max_sampling_length=50, sample=False):
        sampled_ids = []

        if labels_onehot is None:
            labels_onehot = self.convert_onehot(labels)
        image_features = self.linear1(image_features)
        image_features = F.relu(image_features)
        image_features = torch.cat((image_features, labels_onehot), 1)
        image_features = image_features.unsqueeze(1)

        embedded_word = self.word_embed(start_word)
        embedded_word = embedded_word.expand(image_features.size(0), -1, -1)

        lstm1_states, lstm2_states = states

        end_word = end_word.squeeze().expand(image_features.size(0))
        reached_end = torch.zeros_like(end_word.data).byte()

        if sample:
            log_probabilities = []
            lengths = torch.zeros_like(reached_end).long()

        i = 0
        while not reached_end.all() and i < max_sampling_length:
            lstm1_input = embedded_word

            # LSTM 1
            lstm1_output, lstm1_states = self.lstm1(lstm1_input, lstm1_states)

            lstm1_output = torch.cat((image_features, lstm1_output), 2)

            # LSTM 2
            lstm2_output, lstm2_states = self.lstm2(lstm1_output, lstm2_states)

            outputs = self.linear2(lstm2_output.squeeze(1))
            if sample:
                predicted, log_p = self.sample(outputs)
                active_batches = (~reached_end)
                log_p *= to_var(active_batches.float(), log_p.is_cuda)
                log_probabilities.append(log_p.unsqueeze(1))
                lengths += active_batches.long()
            else:
                predicted = outputs.max(1)[1]
            reached_end = reached_end | predicted.eq(end_word).data
            sampled_ids.append(predicted.unsqueeze(1))
            embedded_word = self.word_embed(predicted)
            embedded_word = embedded_word.unsqueeze(1)

            i += 1

        sampled_ids = torch.cat(sampled_ids, 1).squeeze()
        if sample:
            log_probabilities = torch.cat(log_probabilities, 1).squeeze()
            return sampled_ids, log_probabilities, lengths
        return sampled_ids

    def convert_onehot(self, labels):
        labels_onehot = torch.zeros(labels.size(0),
                self.num_classes)
        labels_onehot.scatter_(1, labels.unsqueeze(1), 1)
        return labels_onehot
