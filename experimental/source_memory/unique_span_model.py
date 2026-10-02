"""Deterministic set semantics for repeated, valid selections of one source span."""
import json
from .span_model import SpanAnnotator


class UniqueSpanAnnotator(SpanAnnotator):
    identity=SpanAnnotator.identity+'/strict-schema-unique-span-v5'

    def extract(self,messages,prior):
        targets={p['id'] for p in prior}
        def normalize(request):
            value=json.loads(self.complete(request))
            if not isinstance(value,dict) or set(value)!={'atoms'} or not isinstance(value['atoms'],list) or len(value['atoms'])>12:
                raise ValueError('invalid selection')
            unique=[];seen=set()
            for atom in value['atoms']:
                if not isinstance(atom,dict) or not {'span','kind','key'}<=set(atom) or set(atom)-{'span','kind','key','links'}:
                    raise ValueError('invalid selection fields')
                ident=atom['span']
                if not isinstance(ident,str):raise ValueError('invalid span ID')
                # Check every annotation, including ones that will be deduplicated.
                # A duplicate cannot conceal an invalid relation or annotation.
                if atom['kind'] not in ('fact','preference','recommendation','event','correction'):
                    raise ValueError('invalid kind')
                if not isinstance(atom['key'],str) or not 0<len(atom['key'])<=200:raise ValueError('invalid key')
                links=atom.get('links',[])
                if not isinstance(links,list) or len(links)>4:raise ValueError('invalid links')
                for link in links:
                    if not isinstance(link,dict) or set(link)!={'target','relation'} or link['target'] not in targets or link['relation'] not in ('updates','corrects','supports'):
                        raise ValueError('invalid link')
                if ident not in seen:
                    unique.append(atom);seen.add(ident)
            # First occurrence wins; never merge or invent semantic labels/links.
            return json.dumps({'atoms':unique},ensure_ascii=False)
        return SpanAnnotator(normalize).extract(messages,prior)
