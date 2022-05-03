from transformers import T5EncoderModel
from transformers.modeling_outputs import SequenceClassifierOutput, TokenClassifierOutput

import torch
import torch.nn as nn
from torch.nn import BCEWithLogitsLoss, CrossEntropyLoss, MSELoss
import torch.nn.functional as F
from torchcrf import CRF

from typing import Callable, Dict, Type
import importlib

MODEL_REGISTRY: Dict[str, Type] = {}

def register_model(name: str) -> Callable[[Type], Type]:
    """
    Register an model to be available in command line calls.

    >>> @register_model("my_model")
    ... class My_Model:
    ...     pass
    """

    def _inner(cls_):
        global MODEL_REGISTRY
        MODEL_REGISTRY[name] = cls_
        return cls_

    return _inner


def _camel_case(name: str):
    words = name.split('_')
    class_name = ''
    for w in words:
        class_name += w[0].upper() + w[1:]
    return class_name


def load_model(model_path: str):
    global MODEL_REGISTRY
    if model_path in MODEL_REGISTRY:
        return MODEL_REGISTRY[model_path]

    if ':' in model_path:
        path_list = model_path.split(':')
        module_name = path_list[0]
        class_name = _camel_case(path_list[1])
    elif '/' in model_path:
        path_list = model_path.split(':')
        module_path = path_list[0].split('/')
        module_name = '.'.join(module_path)
        class_name = _camel_case(path_list[1])
    else:
        raise ValueError('unsupported model path: {}. '
        'you have to provide full path to model or '
        'register the model using @register_model decorator'.format(model_path))

    my_module = importlib.import_module(module_name)
    model_class = getattr(my_module, class_name)
    return model_class


class SimplePooler(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.dense1 = nn.Linear(config.d_model, config.d_model)
        self.dense2 = nn.Linear(config.d_model, config.d_model)
        self.dropout = nn.Dropout(config.dropout_rate)

    def forward(self, hidden_states, mask=None):
        # hidden states: [batch_size, seq, model_dim]
        # attention masks: [batch_size, seq, 1]
        first_token_tensor = hidden_states[:, 0]

        pooled_output = self.dense1(first_token_tensor)
        pooled_output = F.relu(pooled_output)
        pooled_output = self.dropout(pooled_output)
        pooled_output = self.dense2(pooled_output)
        
        return pooled_output


class MeanPooler(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.dense1 = nn.Linear(config.d_model, config.d_model)
        self.dense2 = nn.Linear(config.d_model, config.d_model)
        self.dropout = nn.Dropout(config.dropout_rate)

    def forward(self, hidden_states, mask, sqrt=True):
        # hidden states: [batch_size, seq, model_dim]
        # attention masks: [batch_size, seq, 1]
        sentence_sums = torch.bmm(hidden_states.permute(0, 2, 1), mask.float().unsqueeze(-1)).squeeze(-1)
        divisor = mask.sum(dim=1).view(-1, 1).float()
        if sqrt:
            divisor = divisor.sqrt()
        sentence_sums /= divisor

        pooled_output = self.dense1(sentence_sums)
        pooled_output = F.relu(pooled_output)
        pooled_output = self.dropout(pooled_output)
        pooled_output = self.dense2(pooled_output)
        
        return pooled_output

@register_model('T5EncoderForEntityRecognitionWithCRF')
class T5EncoderForEntityRecognitionWithCRF(T5EncoderModel):
    def __init__(self, config):
        if not hasattr(config, 'problem_type'):
            config.problem_type = None
        super(T5EncoderForEntityRecognitionWithCRF, self).__init__(config)

        self.num_labels = config.num_labels

        self.dropout = nn.Dropout(config.dropout_rate)
        self.position_wise_ff = nn.Linear(config.d_model, config.num_labels)
        self.crf = CRF(config.num_labels, batch_first=True)

    def forward(
        self,
        input_ids=None,
        attention_mask=None,
        head_mask=None,
        inputs_embeds=None,
        labels=None,
        output_attentions=None,
        output_hidden_states=None,
        return_dict=None,
    ):
        r"""
        Returns:
        Example::
            >>> from transformers import T5Tokenizer, T5EncoderModel
            >>> tokenizer = T5Tokenizer.from_pretrained('t5-small')
            >>> model = T5EncoderModel.from_pretrained('t5-small')
            >>> input_ids = tokenizer("Studies have been shown that owning a dog is good for you", return_tensors="pt").input_ids  # Batch size 1
            >>> outputs = model(input_ids=input_ids)
            >>> last_hidden_states = outputs.last_hidden_state
        """
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        if attention_mask is None:
            attention_mask = torch.ones_like(input_ids, device=input_ids.device)

        outputs = self.encoder(
            input_ids=input_ids,
            attention_mask=attention_mask,
            inputs_embeds=inputs_embeds,
            head_mask=head_mask,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )

        last_hidden_state = outputs[0]
        last_hidden_state = self.dropout(last_hidden_state)
        emissions = self.position_wise_ff(last_hidden_state)

        loss = None
        if labels is not None:
            mask = attention_mask.to(torch.bool)
            loss = self.crf(emissions, labels, mask=mask)
            loss = -1 * loss
            logits = self.crf.decode(emissions, mask)
        else:
            mask = attention_mask.to(torch.bool)
            logits = self.crf.decode(emissions, mask)

        if not return_dict:
            output = (logits, ) + outputs[2:]
            return ((loss,) + output) if loss is not None else logits

        return TokenClassifierOutput(
            loss=loss,
            logits=logits,
            hidden_states=outputs.hidden_states,
            attentions=outputs.attentions,
        )


@register_model('T5EncoderForCoreferenceResolutionFirstSubmeanObjmeanWithCRF')
class T5EncoderForCoreferenceResolutionFirstSubmeanObjmeanWithCRF(T5EncoderModel):
    def __init__(self, config):
        if not hasattr(config, 'problem_type'):
            config.problem_type = None
        super(T5EncoderForCoreferenceResolutionFirstSubmeanObjmeanWithCRF, self).__init__(config)

        self.num_labels = config.num_labels
        self.model_dim = config.d_model

        self.pooler = SimplePooler(config)
        self.dropout = nn.Dropout(config.dropout_rate)
        self.fc_layer = nn.Linear(self.model_dim, self.model_dim)
        self.classifier = nn.Linear(self.model_dim * 2 ,self.num_labels)
        # self.position_wise_ff = nn.Linear(config.d_model, config.num_labels)
        self.crf = CRF(config.num_labels, batch_first=True)

    def forward(
        self,
        input_ids=None,
        attention_mask=None,
        head_mask=None,
        inputs_embeds=None,
        labels=None,
        output_attentions=None,
        output_hidden_states=None,
        return_dict=None,
        entity_token_idx=None
    ):

        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        if attention_mask is None:
            attention_mask = torch.ones_like(input_ids, device=input_ids.device)

        outputs = self.encoder(
            input_ids=input_ids,
            attention_mask=attention_mask,
            inputs_embeds=inputs_embeds,
            head_mask=head_mask,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )

        last_hidden_state = outputs[0]



        #pooled_output = self.pooler(last_hidden_state, attention_mask)
        #pooled_output = self.dropout(pooled_output)

        sub_output = []
        for b_idx, entity_idx in enumerate(entity_token_idx):
            sub_hidden = last_hidden_state[b_idx,entity_idx[0]:entity_idx[1],:]
            # sub_hidden = [var, model_dim]
            sub_hidden_mean = torch.mean(sub_hidden, 0)
            # sub_hidden_mean = [model_dim]
            sub_output.append(sub_hidden_mean.unsqueeze(0))
            # sub_hidden_mean.unsqueeze = [1, model_dim]

        # sub_output = [batch, model_dim]
        sub_hidden_mean_cat = self.fc_layer(torch.cat((sub_output)))
        # sub_hidden_mean_cat = [batch,  model_dim]
        sub_hidden_mean_cat = self.dropout(sub_hidden_mean_cat)
        # sub_hidden_mean_cat = [batch,  model_dim]
        sub_hidden_mean_cat = sub_hidden_mean_cat.unsqueeze(1).repeat(1,last_hidden_state.size(1),1)
        # sub_hidden_mean_cat = [batch, seq_length, model_dim]
        # last_hidden_state = [batch, seq_length, model_dim]

        entities_concat = torch.cat([last_hidden_state, sub_hidden_mean_cat], dim=-1)
        # entities_concat = [batch, seq_length, model_dim*2]
        
        emissions = self.classifier(entities_concat)
        # entities_concat = [batch, seq_length, model_dim]

        loss = None
        if labels is not None:
            mask = attention_mask.to(torch.bool)
            loss = self.crf(emissions, labels, mask=mask)
            loss = -1 * loss
            logits = self.crf.decode(emissions, mask)
            print(loss)
        else:
            mask = attention_mask.to(torch.bool)
            logits = self.crf.decode(emissions, mask)

        if not return_dict:
            output = (logits, ) + outputs[2:]
            return ((loss,) + output) if loss is not None else logits

        return TokenClassifierOutput(
            loss=loss,
            logits=logits,
            hidden_states=outputs.hidden_states,
            attentions=outputs.attentions,
        )


@register_model('T5EncoderForSequenceClassificationMeanSubmeanObjmean')
class T5EncoderForSequenceClassificationMeanSubmeanObjmean(T5EncoderModel):
    def __init__(self, config):
        if not hasattr(config, 'problem_type'):
            config.problem_type = None
        super(T5EncoderForSequenceClassificationMeanSubmeanObjmean, self).__init__(config)
        self.num_labels = config.num_labels        
        self.model_dim = config.d_model
        
        self.pooler = MeanPooler(config)
        self.dropout = nn.Dropout(config.dropout_rate)
        self.fc_layer = nn.Sequential(nn.Linear(self.model_dim, self.model_dim))
        self.classifier = nn.Sequential(nn.Linear(self.model_dim * 3 ,self.num_labels)
                                       )

    def forward(self,
        input_ids=None,
        attention_mask=None,
        head_mask=None,
        inputs_embeds=None,
        labels=None,
        output_attentions=None,
        output_hidden_states=None,
        return_dict=None,
        entity_token_idx=None
    ):

        outputs = self.encoder(
            input_ids=input_ids,
            attention_mask=attention_mask,
            inputs_embeds=inputs_embeds,
            head_mask=head_mask,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )

        last_hidden_state = outputs[0]
        pooled_output = self.pooler(last_hidden_state, attention_mask)
        pooled_output = self.dropout(pooled_output)

        sub_output = []
        obj_output = []
        for b_idx, entity_idx in enumerate(entity_token_idx):
            sub_entity_idx, obj_entity_idx = entity_idx
            sub_hidden = last_hidden_state[b_idx,sub_entity_idx[0]:sub_entity_idx[1],:]
            sub_hidden_mean = torch.mean(sub_hidden, 0)
            sub_output.append(sub_hidden_mean.unsqueeze(0))
            
            obj_hidden = last_hidden_state[b_idx,obj_entity_idx[0]:obj_entity_idx[1],:]
            obj_hidden_mean = torch.mean(obj_hidden, 0)
            obj_output.append(obj_hidden_mean.unsqueeze(0))
            
        sub_hidden_mean_cat = self.fc_layer(torch.cat((sub_output)))
        obj_hidden_mean_cat = self.fc_layer(torch.cat((obj_output)))

        entities_concat = torch.cat([pooled_output, sub_hidden_mean_cat, obj_hidden_mean_cat], dim=-1)
        
        logits = self.classifier(entities_concat)

        loss = None
        if labels is not None:
            if self.config.problem_type is None:
                if self.num_labels == 1:
                    self.config.problem_type = "regression"
                elif self.num_labels > 1 and (labels.dtype == torch.long or labels.dtype == torch.int):
                    self.config.problem_type = "single_label_classification"
                else:
                    self.config.problem_type = "multi_label_classification"

            if self.config.problem_type == "regression":
                loss_fct = MSELoss()
                loss = loss_fct(logits.view(-1, self.num_labels), labels)
            elif self.config.problem_type == "single_label_classification":
                loss_fct = CrossEntropyLoss()
                loss = loss_fct(logits.view(-1, self.num_labels), labels.view(-1))
            elif self.config.problem_type == "multi_label_classification":
                loss_fct = BCEWithLogitsLoss()
                loss = loss_fct(logits, labels)
        
        return SequenceClassifierOutput(
            loss=loss,
            logits=logits,
            hidden_states=outputs.hidden_states,
            attentions=outputs.attentions,
        )

@register_model('T5EncoderForSequenceClassificationFirstSubmeanObjmean')
class T5EncoderForSequenceClassificationFirstSubmeanObjmean(T5EncoderModel):
    def __init__(self, config):
        if not hasattr(config, 'problem_type'):
            config.problem_type = None
        super(T5EncoderForSequenceClassificationFirstSubmeanObjmean, self).__init__(config)
        self.num_labels = config.num_labels        
        self.model_dim = config.d_model

        self.pooler = SimplePooler(config)
        self.dropout = nn.Dropout(config.dropout_rate)
        self.fc_layer = nn.Sequential(nn.Linear(self.model_dim, self.model_dim))
        self.classifier = nn.Sequential(nn.Linear(self.model_dim * 3 ,self.num_labels)
                                       )

    def forward(self,
        input_ids=None,
        attention_mask=None,
        head_mask=None,
        inputs_embeds=None,
        labels=None,
        output_attentions=None,
        output_hidden_states=None,
        return_dict=None,
        entity_token_idx=None
    ):

        outputs = self.encoder(
            input_ids=input_ids,
            attention_mask=attention_mask,
            inputs_embeds=inputs_embeds,
            head_mask=head_mask,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )

        last_hidden_state = outputs[0]
        pooled_output = self.pooler(last_hidden_state, attention_mask)
        pooled_output = self.dropout(pooled_output)

        sub_output = []
        obj_output = []
        for b_idx, entity_idx in enumerate(entity_token_idx):
            sub_entity_idx, obj_entity_idx = entity_idx
            sub_hidden = last_hidden_state[b_idx,sub_entity_idx[0]:sub_entity_idx[1],:]
            sub_hidden_mean = torch.mean(sub_hidden, 0)
            sub_output.append(sub_hidden_mean.unsqueeze(0))
            
            obj_hidden = last_hidden_state[b_idx,obj_entity_idx[0]:obj_entity_idx[1],:]
            obj_hidden_mean = torch.mean(obj_hidden, 0)
            obj_output.append(obj_hidden_mean.unsqueeze(0))
            
        sub_hidden_mean_cat = self.fc_layer(torch.cat((sub_output)))
        sub_hidden_mean_cat = self.dropout(sub_hidden_mean_cat)
        obj_hidden_mean_cat = self.fc_layer(torch.cat((obj_output)))
        obj_hidden_mean_cat = self.dropout(obj_hidden_mean_cat)

        entities_concat = torch.cat([pooled_output, sub_hidden_mean_cat, obj_hidden_mean_cat], dim=-1)
        
        logits = self.classifier(entities_concat)

        loss = None
        if labels is not None:
            if self.config.problem_type is None:
                if self.num_labels == 1:
                    self.config.problem_type = "regression"
                elif self.num_labels > 1 and (labels.dtype == torch.long or labels.dtype == torch.int):
                    self.config.problem_type = "single_label_classification"
                else:
                    self.config.problem_type = "multi_label_classification"

            if self.config.problem_type == "regression":
                loss_fct = MSELoss()
                loss = loss_fct(logits.view(-1, self.num_labels), labels)
            elif self.config.problem_type == "single_label_classification":
                loss_fct = CrossEntropyLoss()
                loss = loss_fct(logits.view(-1, self.num_labels), labels.view(-1))
            elif self.config.problem_type == "multi_label_classification":
                loss_fct = BCEWithLogitsLoss()
                loss = loss_fct(logits, labels)
        
        return SequenceClassifierOutput(
            loss=loss,
            logits=logits,
            hidden_states=outputs.hidden_states,
            attentions=outputs.attentions,
        )

# 1st_mean + sub_start + obj_start
# 1st_mean + sub_mean + ohb_mean
# 1st_mean + sub_start_end + obj_start_end

# 1st + sub_start + obj_start
# 1st + sub_mean + ohb_mean
# 1st + sub_start_end + obj_start_end
