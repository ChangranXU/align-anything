# Copyright 2024 PKU-Alignment Team. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

import argparse
import json
import os
from huggingface_hub import login
from align_anything.evaluation.inference.vllm_inference import BaseInferencer_vllm
from align_anything.evaluation.dataloader.base_dataloader import BaseDataLoader
from typing import List, Dict, Any
from datasets import load_dataset
from align_anything.utils.tools import read_eval_cfgs, dict_to_namedtuple, update_dict, custom_cfgs_to_dict
from align_anything.utils.template_registry import get_template_class
from align_anything.evaluation.data_type import InferenceInput, InferenceOutput
from align_anything.evaluation.inference.vllm_inference import update_results
from align_anything.evaluation.eval_logger import EvalLogger
import re

class GPQAEvalDataLoader(BaseDataLoader):
    def get_task_names(self):
        if isinstance(self.data_cfgs.task, list):
            return self.data_cfgs.task
        else:
            task_names = [
            self.data_cfgs.task
            ]
            return task_names

    def get_answer(self, data):
        return chr(65 + data['answer'])

    def set_fewshot_dataset(self, dataset, task): 
        if self.cot:
            with open('../cot_fewshot/GPQA/' + task + '.json', 'r', encoding='utf-8') as f:
                data = json.load(f)
            return data
        else:
            return dataset['train']
        
    def build_example_prompt(self, data, with_answer=True, cot=False):
        choices_text = []
        for idx, option in enumerate(data['options']):
            choices_text.append(f'({chr(65 + idx)}): {option}')
        choices = '\n'.join(choices_text)
        answer = f'Answer: {self.get_answer(data)}' if with_answer else 'Answer: '
        return f"{data['Question']}\n{choices}\n{answer}"

    def build_prompt(self, data):
        prompt = """
        Please answer the below question. You should generate ONLY ONE character.\n\n
        """
        cot_prompt = f" Let's think step by step. "
        few_shot_examples = self.few_shot_data[:self.num_shot] if self.num_shot else []
        template = get_template_class(self.chat_template)
        if len(few_shot_examples) == 0:
            question = [template.system_prompt + template.user_prompt.format(input=prompt + self.build_example_prompt(item, False)) + template.assistant_prompt.format(output="") for item in data]
        else:
            if not self.cot:
                few_shots = [
                    self.build_example_prompt(
                        {key: value[i] for key, value in few_shot_examples.items()}, True
                    )
                    for i in range(len(few_shot_examples['Question']))
                ]
            else:
                few_shots = [
                    f"{example['Question']}\n'Answer: '{chr(65+example['answer'])}" for example in few_shot_examples
                ]
            question = []
            for item in data:
                request = {}
                for key, value in item.items():
                    request[key] = value
                examples = few_shots + [self.build_example_prompt(request, False)]
                if self.cot:
                    question.append(template.system_prompt + template.user_prompt.format(input=prompt + '\n\n'.join(examples)) + template.assistant_prompt.format(output=cot_prompt))
                else:
                    question.append(template.system_prompt + template.user_prompt.format(input=prompt + '\n\n'.join(examples)) + template.assistant_prompt.format(output=""))
        
        return question
    
class GPQAEvalGeneratorVLLM(BaseInferencer_vllm):
    def eval(self, data:Dict[str, List[InferenceInput]], eval_configs) -> Dict[str, List[InferenceOutput]]:
        task2details = {}
        for task, input in data.items():
            task2details[task] = self.generation(input)
        
        output_dir = eval_configs.output_dir
        brief_filename = eval_configs.brief_filename
        model_id = self.model_cfgs.model_id
        detailed_filename = f'{model_id}_detailed'
        brief_filename = f'{model_id}_brief'
        update_results(output_dir, brief_filename, detailed_filename,task2details)
        
        return task2details
    
def evaluator(raw_output: List[InferenceOutput], dataloader: GPQAEvalDataLoader, task: str):
    dataset = load_dataset(dataloader.task_dir, task)[dataloader.split]
    correct_answers = []
    responses = []
    true_cases = []
    false_cases = []
    cnt_sum = 0
    cnt_match = 0
    cnt_fail = 0
    flag_fail = True
    for instance in dataset:
        correct_answers.append(
            {
                'prompt': instance['Question'],
                'choices': instance['options'],
                'answer': dataloader.get_answer(instance)
            }
        )
    for item in raw_output:
        dataloader.candidate_labels = get_candidate_labels(item.prompt)
        responses.append(
            {
                'prompt': (item.prompt),
                'answer_logprobs': get_chosen_answer(item.response_logprobs[-1], dataloader.candidate_labels),
                'answer': item.response[0]
            }
        )
    for correct_answer in correct_answers:
        cnt_sum += 1
        for response in responses:
            if correct_answer['prompt'] in response['prompt']:
                flag_fail = False
                chosen_answer = max(response['answer_logprobs'], key=response['answer_logprobs'].get)
                eval_case = {
                    'question': correct_answer['prompt'],
                    'choices': correct_answer['choices'],
                    'correct_answer': correct_answer['answer'],
                    'answer_logprobs': response['answer_logprobs'],
                    'chosen_answer': chosen_answer
                }
                if judge_answer(correct_answer['answer'], chosen_answer, response['answer']):
                    cnt_match += 1
                    eval_case['result'] = True
                    true_cases.append(eval_case)
                else:
                    eval_case['result'] = False
                    false_cases.append(eval_case)
                break
        if flag_fail:
            cnt_fail += 1
        else:
            flag_fail = True
        
    return cnt_match, cnt_sum, true_cases, false_cases

def get_chosen_answer(logprobs: List[Dict[str, Any]], candidate_answers: List[str]):
    answer_logprobs = {}
    for logprob in logprobs:
        key = next(iter(logprob.values())).decoded_token
        value = next(iter(logprob.values())).logprob
        if key in candidate_answers:
            answer_logprobs[key] = value
    for label in candidate_answers:
        if label not in answer_logprobs.keys():
            answer_logprobs[label] = -100.0
    return answer_logprobs

def judge_answer(correct_answer: str, chosen_answer: str, answer: str):
    if correct_answer.lower() == answer.lower() or correct_answer.lower() == chosen_answer.lower():
        return True
    else:
        return False
    
def get_candidate_labels(prompt):
    choices = re.findall(r'\((\w)\)\:', prompt)
    choices = list(set(choices))
    return choices

def main():
    login(token='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')
    parser = argparse.ArgumentParser(description='Evaluation Configuration')

    _, unparsed_args = parser.parse_known_args()
    keys = [k[2:] for k in unparsed_args[0::2]]
    values = list(unparsed_args[1::2])
    unparsed_args = dict(zip(keys, values))
    logger = EvalLogger('Evaluation')

    dict_configs, infer_configs = read_eval_cfgs('gpqa', 'vLLM')

    try:
        assert dict_configs or infer_configs, "Config file does not exist or is incomplete."
    except AssertionError as e:
        logger.log('error', "Config file is not exist or incomplete.")
        exit()
    
    for k, v in unparsed_args.items():
        if v == '' or v is None:
            continue
        dict_configs = update_dict(dict_configs, custom_cfgs_to_dict(k, v))
        infer_configs = update_dict(infer_configs, custom_cfgs_to_dict(k, v))
    
    dict_configs, infer_configs = dict_to_namedtuple(dict_configs), dict_to_namedtuple(infer_configs)
    model_config = dict_configs.default.model_cfgs
    eval_configs = dict_configs.default.eval_cfgs
    dataloader = GPQAEvalDataLoader(dict_configs)
    inferencer = GPQAEvalGeneratorVLLM(model_config,infer_configs)

    data = dataloader.load_dataset()
    raw_outputs = inferencer.eval(data, eval_configs)

    os.makedirs(logger.log_dir, exist_ok=True)

    for task, _ in raw_outputs.items():

        cnt_match, cnt_sum,_,_ = evaluator(raw_outputs[task], dataloader, task)

        eval_results = {
            'model_id': [dict_configs.default.model_cfgs.model_id],
            'num_fewshot': [eval_configs.n_shot],
            'chain_of_thought': [eval_configs.cot],
            'num_match': [cnt_match],
            'num_sum': [cnt_sum],
            'accuracy': [cnt_match / cnt_sum]
        }
        logger.print_table(title=f'GPQA/{task} Benchmark', data=eval_results)
        logger.log('info', '+++++++++++++++++++++++++++++++++++++++++++++++++++++++++++')
        logger.log('info', f"task: {task}")
        logger.log('info', f"model_id: {eval_results['model_id'][0]},")
        logger.log('info', f"num_fewshot: {eval_results['num_fewshot'][0]},")
        logger.log('info', f"chain_of_thought: {eval_results['chain_of_thought'][0]},")
        logger.log('info', f"num_match: {eval_results['num_match'][0]},")
        logger.log('info', f"num_sum: {eval_results['num_sum'][0]},")
        logger.log('info', f"accuracy: {eval_results['accuracy'][0]},")
        logger.log('info', '+++++++++++++++++++++++++++++++++++++++++++++++++++++++++++')


if __name__ == '__main__':
    main()