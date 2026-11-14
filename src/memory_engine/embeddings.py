"""Pinned local ONNX representations; explicit model contracts, no network code."""
import hashlib
import json
from pathlib import Path
import time
import numpy as np


class LocalEmbedder:
    def __init__(self,folder):
        started=time.perf_counter()
        self.folder=Path(folder)
        self.manifest=json.loads((self.folder/'manifest.json').read_text())
        if self.manifest['pooling'] not in ('mean','cls','logit'):raise ValueError('Unsupported pooling contract')
        for name,digest in self.manifest['sha256'].items():
            with (self.folder/name).open('rb') as f:
                if hashlib.file_digest(f,'sha256').hexdigest()!=digest:raise ValueError('Model asset identity mismatch')
        import onnxruntime as ort
        from tokenizers import Tokenizer
        options=ort.SessionOptions();options.intra_op_num_threads=2;options.inter_op_num_threads=1;options.log_severity_level=3
        self.session=ort.InferenceSession(str(self.folder/'onnx/model_quantized.onnx'),sess_options=options,providers=['CPUExecutionProvider'])
        self.tokenizer=Tokenizer.from_file(str(self.folder/'tokenizer.json'));self.tokenizer.no_truncation();self.tokenizer.no_padding()
        self.inputs={i.name for i in self.session.get_inputs()}
        self.dimensions=self.manifest['dimensions']
        self.contract={**self.manifest,'inference_recipe':'full-token-window480-weighted-l2-v1',
            'query_limit':512,'onnxruntime':ort.__version__,'execution_provider':'CPUExecutionProvider'}
        self.space_id=hashlib.sha256(json.dumps(self.contract,sort_keys=True).encode()).hexdigest()
        self.load_seconds=time.perf_counter()-started
        self.calls=0;self.tokens=0;self.seconds=0

    def _run(self,ids):
        ids=np.asarray([ids],dtype=np.int64);mask=np.ones_like(ids)
        feed={'input_ids':ids,'attention_mask':mask}
        if 'token_type_ids' in self.inputs:feed['token_type_ids']=np.zeros_like(ids)
        return self.session.run(None,feed)[0]

    def embed(self,text,mode='passage'):
        if mode not in ('query','passage') or not isinstance(text,str) or len(text.encode())>65536:raise ValueError('Invalid embedding input')
        if self.manifest['pooling']=='logit':raise ValueError('A reranker is not an embedding model')
        started=time.perf_counter();prefix=(mode+': ') if self.manifest['prefixes'] else ''
        all_ids=self.tokenizer.encode(prefix+text,add_special_tokens=False).ids
        prefix_ids=self.tokenizer.encode(prefix,add_special_tokens=False).ids if prefix else []
        if all_ids[:len(prefix_ids)]!=prefix_ids:raise ValueError('Tokenizer prefix contract changed')
        body=all_ids[len(prefix_ids):]
        if mode=='query' and len(all_ids)+2>512:raise ValueError('Query exceeds model window; lexical fallback required')
        windows=[body] if mode=='query' else [body[i:i+480] for i in range(0,len(body),480)] or [[]]
        if len(windows)>160:raise ValueError('Embedding input exceeds bounded coverage')
        vector=np.zeros(self.dimensions,dtype=np.float64)
        for window in windows:
            ids=[self.tokenizer.token_to_id('<s>')]+prefix_ids+window+[self.tokenizer.token_to_id('</s>')]
            hidden=self._run(ids)
            pooled=hidden[0,0] if self.manifest['pooling']=='cls' else hidden[0].mean(axis=0)
            pooled=pooled/max(float(np.linalg.norm(pooled)),1e-12)
            vector+=pooled*max(1,len(window))
            self.tokens+=len(ids)
        vector=vector/max(float(np.linalg.norm(vector)),1e-12)
        if vector.shape!=(self.dimensions,) or not np.isfinite(vector).all():raise ValueError('Invalid local vector')
        self.calls+=1;self.seconds+=time.perf_counter()-started
        return vector.astype('<f4')


class LocalReranker(LocalEmbedder):
    def __init__(self,folder):
        super().__init__(folder)
        if self.manifest['pooling']!='logit':raise ValueError('Expected a cross-encoder model')
        self.contract['inference_recipe']='pair-only-second-overflows-max-logit-v1'
        self.space_id=hashlib.sha256(json.dumps(self.contract,sort_keys=True).encode()).hexdigest()
        self.tokenizer.enable_truncation(max_length=512,strategy='only_second',stride=64)

    def score(self,query,passage):
        encoded=self.tokenizer.encode(query,passage)
        windows=[encoded,*encoded.overflowing]
        if len(windows)>8:raise ValueError('Rerank pair exceeds bounded coverage')
        values=[float(self._run(w.ids).reshape(-1)[0]) for w in windows]
        return max(values)
