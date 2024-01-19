"""This is just a bare runner, with a list of commands to execute"""
import os

commands = [
    "python main.py --data_path ./data/easy_100x20_1.0-1.0/samples/sets --weight_decay 0. "
    "--print_every 32 --augment false --batch 1 --task_epochs 10 --train continual_task --seed 9101 "
    "--cem_emb_size 12 --hamming_margin 2 --triplet_lambda 0. --concept_lambda 0.01 --use_mask fuzzy "
    "--concept_polarization_lambda 0.01 --mask_polarization_lambda 0.01 --min_pos_concepts 3 --n_concepts 20 "
    "--model cnn --output_folder exp --balance true --replay_buffer 50 --replay_lambda 0.1 --lr 0.01 "
    "--store_fuzzy no --device cuda:0 ",
]

for command in commands:
    print("Executing the following command:")
    print("\t" + command)
    os.system(command)
