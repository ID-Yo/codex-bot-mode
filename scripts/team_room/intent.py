"""Strict model decision envelope; execution remains a host scope decision."""
import json


def response(text):
    data = json.loads(text)
    if not isinstance(data,dict) or data.get('decision') not in ('discussion','clarification','unsupported','task'):
        raise ValueError('Invalid Manager intent decision')
    expected = {'decision','text'}
    if data['decision']=='task': expected |= {'title','path','context_paths','authorization'}
    if set(data)!=expected or not isinstance(data['text'],str) or len(data['text'])>16000:
        raise ValueError('Invalid Manager intent envelope')
    if data['decision']=='task':
        if data['text'] or not isinstance(data['authorization'],str) or not data['authorization'].strip():
            raise ValueError('Task needs an owner authorization quotation, not a premature success reply')
        if (any(not isinstance(data[k],str) or not data[k].strip() for k in ('title','path'))
                or not isinstance(data['context_paths'],list)
                or not all(isinstance(p,str) for p in data['context_paths'])):
            raise ValueError('Invalid task scope types')
    elif not data['text'].strip():
        raise ValueError('Manager must answer or ask a useful question')
    return data
