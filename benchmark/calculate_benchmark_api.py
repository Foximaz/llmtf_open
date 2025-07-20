import os
import time
import argparse
import subprocess
import torch.multiprocessing as mp
import torch
from multiprocessing import Queue
from queue import Empty
import json
import requests
from contextlib import closing
import socket

# Определение групп задач (без изменений)
task_groups_math_no_think = [
    {'name': 'doom_math_no_think', 'params': {'dataset_names': 'doom/math', 'few_shot_count': 0, 'max_len': 32000, 'name_suffix': 'no_think', 'batch_size': 100000000000}},
    {'name': 'doom_phys_no_think', 'params': {'dataset_names': 'doom/phys', 'few_shot_count': 0, 'max_len': 32000, 'name_suffix': 'no_think', 'batch_size': 100000000000}},
    {'name': 't-bank_t-math_no_think', 'params': {'dataset_names': 't-bank/t-math', 'few_shot_count': 0, 'max_len': 32000, 'name_suffix': 'no_think', 'batch_size': 100000000000}}
]

task_groups_math_think = [
    {'name': 'doom_math', 'params': {'dataset_names': 'doom/math', 'few_shot_count': 0, 'max_len': 32000, 'name_suffix': 'think', 'batch_size': 100000000000}, 'think': True},
    {'name': 'doom_phys', 'params': {'dataset_names': 'doom/phys', 'few_shot_count': 0, 'max_len': 32000, 'name_suffix': 'think', 'batch_size': 100000000000}, 'think': True},
    {'name': 't-bank_t-math', 'params': {'dataset_names': 't-bank/t-math', 'few_shot_count': 0, 'max_len': 32000, 'name_suffix': 'think', 'batch_size': 100000000000}, 'think': True}
]

# Функция run_eval теперь принимает base_url как явный аргумент
def run_eval(args, group, gen_config_settings, base_url):
    """Запускает один таск оценки модели через API."""
    batch_size = group['params'].get('batch_size', 10000000)
    few_shot_count = group['params'].get('few_shot_count', 0)
    max_len = group['params'].get('max_len', args.max_len)
    name_suffix = group['params'].get('name_suffix', None)

    # Используем переданный base_url вместо args.base_url
    command = ['python', 'evaluate_model_api.py', '--base_url', base_url.replace('/v1', ''), '--model_name_or_path', args.model_dir, '--api_key', args.api_key, '--max_len', str(max_len), '--few_shot_count', str(few_shot_count), '--batch_size', str(batch_size)]
    command += ['--dataset_names'] + group['params']['dataset_names'].split()
    if not group.get('think', False):
        command += ['--disable_thinking']
    if 'max_sample_per_dataset' in group['params']:
        command += ['--max_sample_per_dataset', group['params']['max_sample_per_dataset']]

    if args.output_dir is not None:
        output_dir = args.output_dir
    else:
        output_dir = os.path.join(args.model_dir, 'llmtf_eval')
    command += ['--output_dir', output_dir]
    if args.force_recalc:
        command += ['--force_recalc']

    if name_suffix is not None:
        command += ['--name_suffix', name_suffix]

    group_custom_gen_config = gen_config_settings.get(group['name'], {})
    for param in group_custom_gen_config:
        command += [f'--{param}', str(group_custom_gen_config[param])]

    env = os.environ.copy()
    torchrun_env_names = {'TORCHELASTIC_USE_AGENT_STORE', 'OMP_NUM_THREADS', 'GROUP_RANK', 'ROLE_RANK', 'ROLE_NAME', 'LOCAL_WORLD_SIZE', 'GROUP_WORLD_SIZE', 'ROLE_WORLD_SIZE', 'MASTER_ADDR', 'MASTER_PORT', 'WORLD_SIZE', 'LOCAL_RANK', 'RANK'}
    for var_name in torchrun_env_names:
        if var_name in env:
            del env[var_name]
    command = [str(c) for c in command]
    print(f"[{base_url}] Running command: {' '.join(command)}")

    try:
        subprocess.run(command, env=env, check=True)
    except subprocess.CalledProcessError as e:
        print(f"[{base_url}] Error executing task '{group['name']}': {e}")
        return False

    return True

def read_json(file_name):
    with open(file_name, encoding="utf-8") as r:
        return json.load(r)

# НОВАЯ ФУНКЦИЯ: Воркер, который будет выполняться в отдельном процессе
def worker(worker_id, task_queue, args, gen_config_settings, base_url):
    """
    Функция-воркер. Получает задачи из очереди и выполняет их,
    используя закрепленный за ним base_url.
    """
    print(f"[Worker-{worker_id}] Started, using API at {base_url}")
    while True:
        try:
            # Неблокирующее получение задачи из очереди
            task_group = task_queue.get_nowait()
            print(f"[Worker-{worker_id}] Took task: {task_group['name']}")
            run_eval(args, task_group, gen_config_settings, base_url)
        except Empty:
            # Если очередь пуста, воркер завершает работу
            print(f"[Worker-{worker_id}] No more tasks. Exiting.")
            break
        except Exception as e:
            print(f"[Worker-{worker_id}] An unexpected error occurred: {e}")
            # Можно добавить логику повтора или просто пропустить задачу
            continue

def is_port_in_use(port):
    """Проверяет, занят ли порт."""
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        return s.connect_ex(('localhost', port)) == 0
        
if __name__ == '__main__':
    # Используем 'spawn' для безопасности при работе с CUDA
    mp.set_start_method('spawn', force=True)

    parser = argparse.ArgumentParser(description="Run vLLM servers and distribute evaluation tasks.")
    # Аргументы для запуска серверов
    parser.add_argument('--num_gpus', type=int, default=torch.cuda.device_count())
    parser.add_argument('--tensor_parallel_size', type=int, default=1, help="Tensor parallel size for each vLLM instance.")
    parser.add_argument('--base_port', type=int, default=8000, help="Base port for the first vLLM server.")
    
    # Существующие аргументы
    parser.add_argument('--model_dir', required=True)
    parser.add_argument('--gen_config_settings')
    parser.add_argument('--api_key', default='EMPTY') # vLLM по умолчанию использует 'EMPTY'
    parser.add_argument('--output_dir', default=None)
    parser.add_argument('--force_recalc', action='store_true')
    parser.add_argument('--add_reasoning_tasks', action='store_true')
    parser.add_argument('--max_len', type=int, default=4000)

    # Старый аргумент base_url больше не нужен
    # parser.add_argument('--base_url')

    args = parser.parse_args()
    print("Parsed arguments:", args)
    
    # Проверка корректности аргументов
    if args.num_gpus % args.tensor_parallel_size != 0:
        raise ValueError("`num_gpus` must be divisible by `tensor_parallel_size`")

    num_instances = args.num_gpus // args.tensor_parallel_size
    print(f"Planning to start {num_instances} vLLM instances.")

    # --- 1. Запуск серверов vLLM ---
    servers = []
    server_urls = []
    server_ports = []
    
    for i in range(num_instances):
        port = args.base_port + i
        if is_port_in_use(port):
            print(f"Port {port} is already in use. Please choose a different base_port or free the port.")
            exit(1)
            
        gpus_for_instance = ",".join(map(str, range(i * args.tensor_parallel_size, (i + 1) * args.tensor_parallel_size)))
        
        server_env = os.environ.copy()
        server_env["CUDA_VISIBLE_DEVICES"] = gpus_for_instance
        server_env['VLLM_USE_V1'] = '0'
        #server_env['VLLM_LOGGING_LEVEL'] = 'ERROR'
        
        command = [
            'python', '-m', 'vllm.entrypoints.openai.api_server',
            '--model', args.model_dir,
            '--port', str(port),
            '--tensor-parallel-size', str(args.tensor_parallel_size),
            '--disable-log-requests',
            '--uvicorn-log-level', 'error',
            '--disable-uvicorn-access-log',
            '--disable-log-stats'
        ]
        command += '--gpu-memory-utilization 0.95 --max_seq_len 32000 --max_model_len 32000'.split()
        print(f"Starting vLLM server instance {i+1}/{num_instances} on port {port} with GPUs: {gpus_for_instance}...")
        
        # Запускаем сервер в фоновом режиме
        p = subprocess.Popen(command, env=server_env)
        servers.append(p)
        server_ports.append(port)
        server_urls.append(f"http://localhost:{port}/v1")

    # --- 2. Ожидание готовности серверов ---
    print("\nWaiting for all vLLM servers to be ready...")
    for i, url in enumerate(server_urls):
        retries = 30
        while retries > 0:
            try:
                # Проверяем health-эндпоинт или просто доступность
                requests.get(url.replace("/v1", "/health"), timeout=5)
                print(f"Server on port {server_ports[i]} is ready.")
                break
            except requests.ConnectionError:
                time.sleep(5)
                retries -= 1
                if retries == 0:
                    print(f"Server on port {server_ports[i]} failed to start!")
                    # Завершаем все уже запущенные серверы и выходим
                    for s in servers:
                        s.terminate()
                    exit(1)

    # --- 3. Основная логика выполнения задач ---
    try:
        task_groups = task_groups_math_no_think
        if args.add_reasoning_tasks:
            task_groups += task_groups_math_think
            
        gen_config_settings = read_json(args.gen_config_settings)

        # Создаем и заполняем очередь задач
        task_queue = Queue()
        for task in task_groups:
            task_queue.put(task)

        # Создаем и запускаем процессы-воркеры
        processes = []
        for i in range(num_instances):
            p = mp.Process(target=worker, args=(i, task_queue, args, gen_config_settings, server_urls[i]))
            processes.append(p)
            p.start()

        # Ожидаем завершения всех воркеров
        for p in processes:
            p.join()

        print("\nAll evaluation tasks completed.")

    finally:
        # --- 4. Завершение работы серверов ---
        print("\nShutting down vLLM servers...")
        for i, server_process in enumerate(servers):
            print(f"Terminating server on port {server_ports[i]}...")
            server_process.terminate()
        # Даем время на завершение
        time.sleep(5)
        for i, server_process in enumerate(servers):
            if server_process.poll() is None: # Если процесс все еще жив
                print(f"Forcefully killing server on port {server_ports[i]}...")
                server_process.kill()
        print("All servers have been shut down.")