from sklearn.metrics import roc_auc_score, f1_score, average_precision_score, precision_score, recall_score, accuracy_score
import os
import matplotlib.pyplot as plt
from copy import deepcopy 
import numpy as np 
from tqdm import tqdm 
import torch 
torch.manual_seed(0)
from torch import nn
from torch.autograd import Variable
import torch.nn.functional as F
from HINT.module import Highway, GCN 
from functools import reduce 
import pickle



class Interaction(nn.Sequential):
	def __init__(self, molecule_encoder, disease_encoder, protocol_encoder, 
					device, 
					global_embed_size, 
					highway_num_layer,
					prefix_name, 
					epoch = 20,
					lr = 3e-4, 
					weight_decay = 0, 
					):
		super(Interaction, self).__init__()
		self.molecule_encoder = molecule_encoder 
		self.disease_encoder = disease_encoder 
		self.protocol_encoder = protocol_encoder 
		self.global_embed_size = global_embed_size 
		self.highway_num_layer = highway_num_layer 
		self.feature_dim = self.molecule_encoder.embedding_size + self.disease_encoder.embedding_size + self.protocol_encoder.embedding_size
		self.epoch = epoch 
		self.lr = lr 
		self.weight_decay = weight_decay 
		self.save_name = prefix_name + '_interaction'

		self.f = F.relu
		self.loss = nn.BCEWithLogitsLoss()

		##### NN 
		self.encoder2interaction_fc = nn.Linear(self.feature_dim, self.global_embed_size).to(device)
		self.encoder2interaction_highway = Highway(self.global_embed_size, self.highway_num_layer).to(device)
		self.pred_nn = nn.Linear(self.global_embed_size, 1)

		self.device = device 
		self = self.to(device)

	def feed_lst_of_module(self, input_feature, lst_of_module):
		x = input_feature
		for single_module in lst_of_module:
			x = self.f(single_module(x))
		return x

	def forward_get_three_encoders(self, smiles_lst2, icdcode_lst3, criteria_lst):
		molecule_embed = self.molecule_encoder.forward_smiles_lst_lst(smiles_lst2)
		icd_embed = self.disease_encoder.forward_code_lst3(icdcode_lst3)
		protocol_embed = self.protocol_encoder.forward(criteria_lst)
		return molecule_embed, icd_embed, protocol_embed	

	def forward_encoder_2_interaction(self, molecule_embed, icd_embed, protocol_embed):
		encoder_embedding = torch.cat([molecule_embed, icd_embed, protocol_embed], 1)
		# interaction_embedding = self.feed_lst_of_module(encoder_embedding, [self.encoder2interaction_fc, self.encoder2interaction_highway])
		h = self.encoder2interaction_fc(encoder_embedding)
		h = self.f(h)
		h = self.encoder2interaction_highway(h)
		interaction_embedding = self.f(h)
		return interaction_embedding 

	def forward(self, smiles_lst2, icdcode_lst3, criteria_lst):
		molecule_embed, icd_embed, protocol_embed = self.forward_get_three_encoders(smiles_lst2, icdcode_lst3, criteria_lst)
		interaction_embedding = self.forward_encoder_2_interaction(molecule_embed, icd_embed, protocol_embed)
		output = self.pred_nn(interaction_embedding)
		return output ### 32, 1

	def evaluation(self, predict_all, label_all, threshold = 0.5):
		import pickle, os
		from sklearn.metrics import roc_curve, precision_recall_curve
		with open("predict_label.txt", 'w') as fout:
			for i,j in zip(predict_all, label_all):
				fout.write(str(i)[:4] + '\t' + str(j)[:4]+'\n')
		auc_score = roc_auc_score(label_all, predict_all)
		figure_folder = "figure"
		#### ROC-curve 
		fpr, tpr, thresholds = roc_curve(label_all, predict_all, pos_label=1)
		# roc_curve =plt.figure()
		# plt.plot(fpr,tpr,'-',label=self.save_name + ' ROC Curve ')
		# plt.legend(fontsize = 15)
		#plt.savefig(os.path.join(figure_folder,name+"_roc_curve.png"))
		#### PR-curve
		precision, recall, thresholds = precision_recall_curve(label_all, predict_all)
		# plt.plot(recall,precision, label = self.save_name + ' PR Curve')
		# plt.legend(fontsize = 15)
		# plt.savefig(os.path.join(figure_folder,self.save_name + "_pr_curve.png"))
		label_all = [int(i) for i in label_all]
		float2binary = lambda x:0 if x<threshold else 1
		predict_all = list(map(float2binary, predict_all))
		f1score = f1_score(label_all, predict_all)
		prauc_score = average_precision_score(label_all, predict_all)
		# print(predict_all)
		precision = precision_score(label_all, predict_all)
		recall = recall_score(label_all, predict_all)
		accuracy = accuracy_score(label_all, predict_all)
		predict_1_ratio = sum(predict_all) / len(predict_all)
		label_1_ratio = sum(label_all) / len(label_all)
		return auc_score, f1score, prauc_score, precision, recall, accuracy, predict_1_ratio, label_1_ratio 

	def testloader_to_lst(self, dataloader):
		nctid_lst, label_lst, smiles_lst2, icdcode_lst3, criteria_lst = [], [], [], [], []
		for nctid, label, smiles, icdcode, criteria in dataloader:
			nctid_lst.extend(nctid)
			label_lst.extend([i.item() for i in label])
			smiles_lst2.extend(smiles)
			icdcode_lst3.extend(icdcode)
			criteria_lst.extend(criteria)
		length = len(nctid_lst)
		assert length == len(smiles_lst2) and length == len(icdcode_lst3)
		return nctid_lst, label_lst, smiles_lst2, icdcode_lst3, criteria_lst, length 



	def generate_predict(self, dataloader):
		whole_loss = 0 
		label_all, predict_all, nctid_all = [], [], []
		for nctid_lst, label_vec, smiles_lst2, icdcode_lst3, criteria_lst in dataloader:
			nctid_all.extend(nctid_lst)
			label_vec = label_vec.to(self.device)
			output = self.forward(smiles_lst2, icdcode_lst3, criteria_lst).view(-1)  
			loss = self.loss(output, label_vec.float())
			whole_loss += loss.item()
			predict_all.extend([i.item() for i in torch.sigmoid(output)])
			label_all.extend([i.item() for i in label_vec])

		return whole_loss, predict_all, label_all, nctid_all

	## MODIFICATION: we have changed this function to also print out precision & accuracy,
    ## fixed prefix_name usage, and included ground truth label in the pickle output.
	def bootstrap_test(self, dataloader, sample_num = 20): #iter
		# if validloader is not None:
		# 	best_threshold = self.select_threshold_for_binary(validloader)
		self.eval()
		best_threshold = 0.5 
		whole_loss, predict_all, label_all, nctid_all = self.generate_predict(dataloader)
		""" DON'T NEED TO PLOT HISTOGRAM AT THE MOMENT
		from HINT.utils import plot_hist
		plt.clf()
		prefix_name = "./figure/" + self.save_name 
		plot_hist(prefix_name, predict_all, label_all)
		"""		
		def bootstrap(length, sample_num):
			idx = [i for i in range(length)]
			from random import choices 
			bootstrap_idx = [choices(idx, k = length) for i in range(sample_num)]
			return bootstrap_idx 
		results_lst = []
		bootstrap_idx_lst = bootstrap(len(predict_all), sample_num = sample_num)
		for bootstrap_idx in bootstrap_idx_lst: 
			bootstrap_label = [label_all[idx] for idx in bootstrap_idx]		
			bootstrap_predict = [predict_all[idx] for idx in bootstrap_idx]
			results = self.evaluation(bootstrap_predict, bootstrap_label, threshold = best_threshold)
			results_lst.append(results)

		self.train() 
		auc = [r[0] for r in results_lst]
		f1score = [r[1] for r in results_lst]
		prauc_score = [r[2] for r in results_lst]
		precision_vals = [r[3] for r in results_lst]
		accuracy_vals  = [r[5] for r in results_lst]
		
		stats = {"auc_mean": np.mean(auc), "auc_std": np.std(auc), "f1_mean": np.mean(f1score), "f1_std": np.std(f1score), 
		   "prauc_mean": np.mean(prauc_score), "prauc_std": np.std(prauc_score), "precision_mean": np.mean(precision_vals), 
		   "precision_std": np.std(precision_vals), "accuracy_mean": np.mean(accuracy_vals), "accuracy_std": np.std(accuracy_vals)}

		print("PR-AUC   mean: "+str(np.mean(prauc_score))[:6], "std: "+str(np.std(prauc_score))[:6])
		print("F1       mean: "+str(np.mean(f1score))[:6], "std: "+str(np.std(f1score))[:6])
		print("ROC-AUC  mean: "+ str(np.mean(auc))[:6],     "std: "+str(np.std(auc))[:6])
		print("Precision mean: "+str(np.mean(precision_vals))[:6], "std: "+str(np.std(precision_vals))[:6])
		print("Accuracy  mean: "+str(np.mean(accuracy_vals))[:6],  "std: "+str(np.std(accuracy_vals))[:6])


		""" I DON'T NEED TO SEE EVERY WRONG TRIAL
		for nctid, label, predict in zip(nctid_all, label_all, predict_all):
			if (predict > 0.5 and label == 0) or (predict < 0.5 and label == 1):
				print(nctid, label, str(predict)[:5])
				"""

		## MODIFICATION: include ground truth label in the pickle and fix the prefix name usage
		nctid2predict = {nctid: (pred, lab) for nctid, pred, lab in zip(nctid_all, predict_all, label_all)} 
		pickle.dump(nctid2predict, open(os.path.join("..", 'results', f'{self.save_name}.pkl'), 'wb')) #{iter}_

		return nctid_all, predict_all, stats

	def ongoing_test(self, dataloader, sample_num = 20):
		self.eval()
		best_threshold = 0.5 
		whole_loss, predict_all, label_all, nctid_all = self.generate_predict(dataloader) 
		self.train() 
		return nctid_all, predict_all 

	def test(self, dataloader, return_loss = True, validloader=None):
		# if validloader is not None:
		# 	best_threshold = self.select_threshold_for_binary(validloader)
		self.eval()
		best_threshold = 0.5 
		whole_loss, predict_all, label_all, nctid_all = self.generate_predict(dataloader)
		# from HINT.utils import plot_hist
		# plt.clf()
		# prefix_name = "./figure/" + self.save_name 
		# plot_hist(prefix_name, predict_all, label_all)
		self.train()
		if return_loss:
			return whole_loss
		else:
			print_num = 5
			auc_score, f1score, prauc_score, precision, recall, accuracy, \
			predict_1_ratio, label_1_ratio = self.evaluation(predict_all, label_all, threshold = best_threshold)
			print("ROC AUC: " + str(auc_score)[:print_num] + "\nF1: " + str(f1score)[:print_num] \
				 + "\nPR-AUC: " + str(prauc_score)[:print_num] \
				 + "\nPrecision: " + str(precision)[:print_num] \
				 + "\nrecall: "+str(recall)[:print_num] + "\naccuracy: "+str(accuracy)[:print_num] \
				 + "\npredict 1 ratio: " + str(predict_1_ratio)[:print_num] \
				 + "\nlabel 1 ratio: " + str(label_1_ratio)[:print_num])
			return auc_score, f1score, prauc_score, precision, recall, accuracy, predict_1_ratio, label_1_ratio 

	def learn(self, train_loader, valid_loader, test_loader):
		opt = torch.optim.Adam(self.parameters(), lr = self.lr, weight_decay = self.weight_decay)
		train_loss_record = [] 
		valid_loss = self.test(valid_loader, return_loss=True)
		valid_loss_record = [valid_loss]
		best_valid_loss = valid_loss
		best_model = deepcopy(self)
		for ep in tqdm(range(self.epoch)):
			for nctid_lst, label_vec, smiles_lst2, icdcode_lst3, criteria_lst in train_loader:
				label_vec = label_vec.to(self.device)
				output = self.forward(smiles_lst2, icdcode_lst3, criteria_lst).view(-1)  #### 32, 1 -> 32, ||  label_vec 32,
				loss = self.loss(output, label_vec.float())
				train_loss_record.append(loss.item())
				opt.zero_grad() 
				loss.backward() 
				opt.step()
			valid_loss = self.test(valid_loader, return_loss=True)
			valid_loss_record.append(valid_loss)
			if valid_loss < best_valid_loss:
				best_valid_loss = valid_loss 
				best_model = deepcopy(self)

		#self.plot_learning_curve(train_loss_record, valid_loss_record)
		self = deepcopy(best_model)
		auc_score, f1score, prauc_score, precision, recall, accuracy, predict_1_ratio, label_1_ratio = self.test(test_loader, return_loss = False, validloader = valid_loader)


	def plot_learning_curve(self, train_loss_record, valid_loss_record):
		plt.plot(train_loss_record)
		plt.savefig("./figure/" + self.save_name + '_train_loss.jpg')
		plt.clf() 
		plt.plot(valid_loss_record)
		plt.savefig("./figure/" + self.save_name + '_valid_loss.jpg')
		plt.clf() 

	def select_threshold_for_binary(self, validloader):
		_, prediction, label_all, nctid_all = self.generate_predict(validloader)
		best_f1 = 0
		for threshold in prediction:
			float2binary = lambda x:0 if x<threshold else 1
			predict_all = list(map(float2binary, prediction))
			f1score = precision_score(label_all, predict_all)        
			if f1score > best_f1:
				best_f1 = f1score 
				best_threshold = threshold
		return best_threshold 









class HINTModel_multi(Interaction):

	def __init__(self, molecule_encoder, disease_encoder, protocol_encoder, 
					device, 
					global_embed_size, 
					highway_num_layer,
					prefix_name, 
					epoch = 20,
					lr = 3e-4, 
					weight_decay = 0,
					):
		super(HINTModel_multi, self).__init__(molecule_encoder = molecule_encoder, 
								   disease_encoder = disease_encoder, 
								   protocol_encoder = protocol_encoder, 
								   device = device, 
								   prefix_name = prefix_name, 
								   global_embed_size = global_embed_size, 
								   highway_num_layer = highway_num_layer,
								   epoch = epoch,
								   lr = lr, 
								   weight_decay = weight_decay)
		self.pred_nn = nn.Linear(self.global_embed_size, 4)
		self.loss = nn.CrossEntropyLoss()

	def forward(self, smiles_lst2, icdcode_lst3, criteria_lst):
		molecule_embed, icd_embed, protocol_embed = self.forward_get_three_encoders(smiles_lst2, icdcode_lst3, criteria_lst)
		interaction_embedding = self.forward_encoder_2_interaction(molecule_embed, icd_embed, protocol_embed)
		output = self.pred_nn(interaction_embedding)
		return output ### 32, 4


	def generate_predict(self, dataloader):
		whole_loss = 0 
		label_all, predict_all = [], []
		for nctid_lst, label_vec, smiles_lst2, icdcode_lst3, criteria_lst in dataloader:
			label_vec = label_vec.to(self.device)
			output = self.forward(smiles_lst2, icdcode_lst3, criteria_lst) 
			loss = self.loss(output, label_vec)
			whole_loss += loss.item()
			predict_all.extend(torch.argmax(output, 1).tolist())
			# predict_all.extend([i.item() for i in torch.sigmoid(output)])
			label_all.extend([i.item() for i in label_vec])

		accuracy = len(list(filter(lambda x:x[0]==x[1], zip(predict_all, label_all)))) / len(label_all)
		return whole_loss, predict_all, label_all, accuracy



	def test(self, dataloader, return_loss = True, validloader=None):
		# if validloader is not None:
		# 	best_threshold = self.select_threshold_for_binary(validloader)
		self.eval()
		whole_loss, predict_all, label_all, accuracy = self.generate_predict(dataloader)
		self.train()
		return whole_loss, predict_all, label_all, accuracy
		# # from HINT.utils import plot_hist
		# # plt.clf()
		# # prefix_name = "./figure/" + self.save_name 
		# # plot_hist(prefix_name, predict_all, label_all)
		# self.train()
		# if return_loss:
		# 	return whole_loss
		# else:
		# 	print_num = 5
		# 	auc_score, f1score, prauc_score, precision, recall, accuracy, \
		# 	predict_1_ratio, label_1_ratio = self.evaluation(predict_all, label_all, threshold = best_threshold)
		# 	print("ROC AUC: " + str(auc_score)[:print_num] + "\nF1: " + str(f1score)[:print_num] \
		# 		 + "\nPR-AUC: " + str(prauc_score)[:print_num] \
		# 		 + "\nPrecision: " + str(precision)[:print_num] \
		# 		 + "\nrecall: "+str(recall)[:print_num] + "\naccuracy: "+str(accuracy)[:print_num] \
		# 		 + "\npredict 1 ratio: " + str(predict_1_ratio)[:print_num] \
		# 		 + "\nlabel 1 ratio: " + str(label_1_ratio)[:print_num])
		# 	return auc_score, f1score, prauc_score, precision, recall, accuracy, predict_1_ratio, label_1_ratio 





	def learn(self, train_loader, valid_loader, test_loader):
		opt = torch.optim.Adam(self.parameters(), lr = self.lr, weight_decay = self.weight_decay)
		train_loss_record = []
		valid_loss, predict_all, label_all, accuracy = self.test(valid_loader, return_loss=True)
		print('accuracy', accuracy)
		# valid_loss_record = [valid_loss]
		# best_valid_loss = valid_loss
		best_model = deepcopy(self)
		for ep in tqdm(range(self.epoch)):
			self.train() 
			for nctid_lst, label_vec, smiles_lst2, icdcode_lst3, criteria_lst in train_loader:
				label_vec = label_vec.to(self.device)
				output = self.forward(smiles_lst2, icdcode_lst3, criteria_lst)  #### 32, 1 -> 32, ||  label_vec 32,
				# print(label_vec.shape, output.shape, label_vec, output)
				loss = self.loss(output, label_vec)
				train_loss_record.append(loss.item())
				opt.zero_grad() 
				loss.backward() 
				opt.step()
			valid_loss, predict_all, label_all, accuracy = self.test(valid_loader, return_loss=True)
			print('accuracy', accuracy)
		return predict_all, label_all
		# 	valid_loss_record.append(valid_loss)
		# 	if valid_loss < best_valid_loss:
		# 		best_valid_loss = valid_loss 
		# 		best_model = deepcopy(self)

		# self.plot_learning_curve(train_loss_record, valid_loss_record)
		# self = deepcopy(best_model)
		# auc_score, f1score, prauc_score, precision, recall, accuracy, predict_1_ratio, label_1_ratio = self.test(test_loader, return_loss = False, validloader = valid_loader)
























class HINT_nograph(Interaction):
	def __init__(self, molecule_encoder, disease_encoder, protocol_encoder, device, 
					global_embed_size, 
					highway_num_layer,
					prefix_name, 
					epoch = 20,
					lr = 3e-4, 
					weight_decay = 0, ):
		super(HINT_nograph, self).__init__(molecule_encoder = molecule_encoder, 
								   disease_encoder = disease_encoder, 
								   protocol_encoder = protocol_encoder,
								   device = device,  
								   global_embed_size = global_embed_size, 
								   prefix_name = prefix_name, 
								   highway_num_layer = highway_num_layer,
								   epoch = epoch,
								   lr = lr, 
								   weight_decay = weight_decay, 
								   ) 
		self.save_name = prefix_name + '_HINT_nograph'
		'''	### interaction model 
		self.molecule_encoder = molecule_encoder 
		self.disease_encoder = disease_encoder 
		self.protocol_encoder = protocol_encoder 
		self.global_embed_size = global_embed_size 
		self.highway_num_layer = highway_num_layer 
		self.feature_dim = self.molecule_encoder.embedding_size + self.disease_encoder.embedding_size + self.protocol_encoder.embedding_size
		self.epoch = epoch 
		self.lr = lr 
		self.weight_decay = weight_decay 
		self.save_name = save_name

		self.f = F.relu
		self.loss = nn.BCEWithLogitsLoss()

		##### NN 
		self.encoder2interaction_fc = nn.Linear(self.feature_dim, self.global_embed_size)
		self.encoder2interaction_highway = Highway(self.global_embed_size, self.highway_num_layer)
		self.pred_nn = nn.Linear(self.global_embed_size, 1)
		'''


		#### risk of disease 
		self.risk_disease_fc = nn.Linear(self.disease_encoder.embedding_size, self.global_embed_size)
		self.risk_disease_higway = Highway(self.global_embed_size, self.highway_num_layer)

		#### augment interaction 
		self.augment_interaction_fc = nn.Linear(self.global_embed_size*2, self.global_embed_size)
		self.augment_interaction_highway = Highway(self.global_embed_size, self.highway_num_layer)

		#### ADMET 
		self.admet_model = []
		for i in range(5):
			admet_fc = nn.Linear(self.molecule_encoder.embedding_size, self.global_embed_size).to(device)
			admet_highway = Highway(self.global_embed_size, self.highway_num_layer).to(device)
			self.admet_model.append(nn.ModuleList([admet_fc, admet_highway])) 
		self.admet_model = nn.ModuleList(self.admet_model)

		#### PK 
		self.pk_fc = nn.Linear(self.global_embed_size*5, self.global_embed_size)
		self.pk_highway = Highway(self.global_embed_size, self.highway_num_layer)

		#### trial node 
		self.trial_fc = nn.Linear(self.global_embed_size*2, self.global_embed_size)
		self.trial_highway = Highway(self.global_embed_size, self.highway_num_layer)

		## self.pred_nn = nn.Linear(self.global_embed_size, 1)

		self.device = device 
		self = self.to(device)


	def forward(self, smiles_lst2, icdcode_lst3, criteria_lst, if_gnn = False):
		### encoder for molecule, disease and protocol
		molecule_embed, icd_embed, protocol_embed = self.forward_get_three_encoders(smiles_lst2, icdcode_lst3, criteria_lst)
		### interaction 
		interaction_embedding = self.forward_encoder_2_interaction(molecule_embed, icd_embed, protocol_embed)
		### risk of disease 
		risk_of_disease_embedding = self.feed_lst_of_module(input_feature = icd_embed, 
															lst_of_module = [self.risk_disease_fc, self.risk_disease_higway])
		### augment interaction   
		augment_interaction_input = torch.cat([interaction_embedding, risk_of_disease_embedding], 1)
		augment_interaction_embedding = self.feed_lst_of_module(input_feature = augment_interaction_input, 
																lst_of_module = [self.augment_interaction_fc, self.augment_interaction_highway])
		### admet 
		admet_embedding_lst = []
		for idx in range(5):
			admet_embedding = self.feed_lst_of_module(input_feature = molecule_embed, 
													  lst_of_module = self.admet_model[idx])
			admet_embedding_lst.append(admet_embedding)
		### pk 
		pk_input = torch.cat(admet_embedding_lst, 1)
		pk_embedding = self.feed_lst_of_module(input_feature = pk_input, 
											   lst_of_module = [self.pk_fc, self.pk_highway])
		### trial 
		trial_input = torch.cat([pk_embedding, augment_interaction_embedding], 1)
		trial_embedding = self.feed_lst_of_module(input_feature = trial_input, 
												  lst_of_module = [self.trial_fc, self.trial_highway])
		output = self.pred_nn(trial_embedding)
		if if_gnn == False:
			return output 
		else:
			embedding_lst = [molecule_embed, icd_embed, protocol_embed, interaction_embedding, risk_of_disease_embedding, \
							 augment_interaction_embedding] + admet_embedding_lst + [pk_embedding, trial_embedding]
			return embedding_lst

class HINTModel(HINT_nograph):

	def __init__(self, molecule_encoder, disease_encoder, protocol_encoder, 
					device, 
					global_embed_size, 
					highway_num_layer,
					prefix_name, 
					gnn_hidden_size, 
					epoch = 20,
					lr = 3e-4, 
					weight_decay = 0,):
		super(HINTModel, self).__init__(molecule_encoder = molecule_encoder, 
								   disease_encoder = disease_encoder, 
								   protocol_encoder = protocol_encoder, 
								   device = device, 
								   prefix_name = prefix_name, 
								   global_embed_size = global_embed_size, 
								   highway_num_layer = highway_num_layer,
								   epoch = epoch,
								   lr = lr, 
								   weight_decay = weight_decay)
		self.save_name = prefix_name 
		self.gnn_hidden_size = gnn_hidden_size 
		#### GNN 
		self.adj = self.generate_adj()          
		self.gnn = GCN(
            nfeat = self.global_embed_size,
            nhid = self.gnn_hidden_size,
            nclass = 1,
            dropout = 0.6,
		    init = 'uniform') 
		### gnn's attention 		
		self.node_size = self.adj.shape[0]
		'''
		self.graph_attention_model_mat = nn.ModuleList([nn.ModuleList([self.gnn_attention() \
        								if self.adj[i,j]==1 else None  \
        								for j in range(self.node_size)]) \
        								for i in range(self.node_size)])
        '''
		self.graph_attention_model_mat = nn.ModuleList([nn.ModuleList([self.gnn_attention() if self.adj[i,j]==1 else None for j in range(self.node_size)]) for i in range(self.node_size)])
		# self.graph_attention_model_mat = nn.ModuleList([nn.ModuleList([self.gnn_attention() if self.adj[i,j]==1 else None for j in range(self.node_size)]) for i in range(self.node_size)])

		'''
nn.ModuleList([ nn.ModuleList([nn.Linear(3,2) for j in range(5)] + [None]) for i in range(3)])
		'''

		self.device = device 
		self = self.to(device)

	def generate_adj(self):        								
		##### consistent with HINT_nograph.forward
		lst = ["molecule", "disease", "criteria", 'INTERACTION', 'risk_disease', 'augment_interaction', 'A', 'D', 'M', 'E', 'T', 'PK', "final"]
		edge_lst = [("disease", "molecule"), ("disease", "criteria"), ("molecule", "criteria"), 
					("disease", "INTERACTION"), ("molecule", "INTERACTION"),  ("criteria", "INTERACTION"), 
					("disease", "risk_disease"), ('risk_disease', 'augment_interaction'), ('INTERACTION', 'augment_interaction'),
					("molecule", "A"), ("molecule", "D"), ("molecule", "M"), ("molecule", "E"), ("molecule", "T"),
					('A', 'PK'), ('D', 'PK'), ('M', 'PK'), ('E', 'PK'), ('T', 'PK'), 
					('augment_interaction', 'final'), ('PK', 'final')]
		adj = torch.zeros(len(lst), len(lst))
		adj = torch.eye(len(lst)) * len(lst)
		num2str = {k:v for k,v in enumerate(lst)}
		str2num = {v:k for k,v in enumerate(lst)}
		for i,j in edge_lst:
			n1,n2 = str2num[i], str2num[j]
			adj[n1,n2] = 1
			adj[n2,n1] = 1
		return adj.to(self.device) 

	def generate_attention_matrx(self, node_feature_mat):
		attention_mat = torch.zeros(self.node_size, self.node_size).to(self.device)
		for i in range(self.node_size):
			for j in range(self.node_size):
				if self.adj[i,j]!=1:
					continue 
				feature = torch.cat([node_feature_mat[i].view(1,-1), node_feature_mat[j].view(1,-1)], 1)
				attention_model = self.graph_attention_model_mat[i][j]
				attention_mat[i,j] = torch.sigmoid(self.feed_lst_of_module(input_feature=feature, lst_of_module=attention_model))
		return attention_mat 

	##### self.global_embed_size*2 -> 1 
	def gnn_attention(self):
		highway_nn = Highway(size = self.global_embed_size*2, num_layers = self.highway_num_layer).to(self.device)
		highway_fc = nn.Linear(self.global_embed_size*2, 1).to(self.device)
		return nn.ModuleList([highway_nn, highway_fc])	

	# added return_embeddings flag here to enable MSE gradient calculations
	def forward(self, smiles_lst2, icdcode_lst3, criteria_lst, return_attention_matrix = False, return_embeddings=False):
		embedding_lst = HINT_nograph.forward(self, smiles_lst2, icdcode_lst3, criteria_lst, if_gnn = True)

		for embedding in embedding_lst: 
			embedding.requires_grad_(True)

		### length is 13, each is 32,50 
		batch_size = embedding_lst[0].shape[0]

		output_lst = []
		if return_attention_matrix:
			attention_mat_lst = []

		for i in range(batch_size):
			node_feature_lst = [embedding[i].view(1,-1) for embedding in embedding_lst]
			node_feature_mat = torch.cat(node_feature_lst, 0) ### 13, 50 
			attention_mat = self.generate_attention_matrx(node_feature_mat)
			output = self.gnn(node_feature_mat, self.adj * attention_mat)
			output = output[-1].view(1,-1)
			output_lst.append(output)

			if return_attention_matrix:
				attention_mat_lst.append(attention_mat)

		output_mat = torch.cat(output_lst, 0)

		if return_embeddings:

			if not return_attention_matrix:
				return output_mat, embedding_lst
			else:
				return output_mat, attention_mat_lst, embedding_lst
		
		else:

			if not return_attention_matrix:
				return output_mat
			else:
				return output_mat, attention_mat_lst

	def interpret(self, complete_dataloader):
		from graph_visualize_interpret import data2graph 
		from HINT.utils import replace_strange_symbol
		for nctid_lst, status_lst, why_stop_lst, label_vec, phase_lst, \
			diseases_lst, icdcode_lst3, drugs_lst, smiles_lst2, criteria_lst in complete_dataloader: 
			output, attention_mat_lst = self.forward(smiles_lst2, icdcode_lst3, criteria_lst, return_attention_matrix=True)
			output = output.view(-1)
			batch_size = len(nctid_lst)
			for i in range(batch_size):
				name = '__'.join([nctid_lst[i], status_lst[i], why_stop_lst[i], \
														str(label_vec[i].item()), str(torch.sigmoid(output[i]).item())[:5], \
														phase_lst[i], diseases_lst[i], drugs_lst[i]])
				if len(name) > 150:
					name = name[:250]
				name = replace_strange_symbol(name)
				name = name.replace('__', '_')
				name = name.replace('  ', ' ')
				name = 'interpret_result/' + name + '.png'
				print(name)
				data2graph(attention_matrix = attention_mat_lst[i], adj = self.adj, save_name = name)

	def init_pretrain(self, admet_model):
		self.molecule_encoder = admet_model.molecule_encoder

	### generate attention matrix


class HINTClassroom:

	def __init__(self, student_model, teacher_model, device, _lambda=0.9, d=0):
		"""
		student_model: instance of HINTModel trained normally
		teacher_model: instance of HINTModel using combined loss
		_lambda: base hyperparameter for convex combination of BCE, MSE losses
		d: dynamically adjusting lambda
		"""
		# the strategy here is not to inherit any model classes,
		# instead, we create specific instances of models, and then use HINTModel's methods
		# as the classroom analogy would suggest - classrooms are forums for learning, not the people themselves
		
		self.student = student_model.to(device)
		self.teacher = teacher_model.to(device)
		self._lambda = _lambda
		self.d = d
		self.device = device
		self.student.train()
		self.teacher.train()
	
	def get_encoder_embedding(self, model, smiles_lst2, icdcode_lst3, criteria_lst):
		"""
		Use model's forward_get_three_encoders to extract domain-specific embeddings,
		then concatenates them to generate a single feature vector per example
		"""
		molecule_embed, icd_embed, protocol_embed = model.forward_get_three_encoders(smiles_lst2, icdcode_lst3, criteria_lst)
		encoder_embedding = torch.cat([molecule_embed, icd_embed, protocol_embed], dim=1)
		return encoder_embedding
	
	def student_test(self, test_loader):
		return self.student.test(test_loader, return_loss=False)
	
	def teacher_test(self, test_loader):
		return self.teacher.test(test_loader, return_loss=False)
	
	def validate_student(self, valid_loader):
		self.student.eval()
		total_loss, count = 0, 0
		with torch.no_grad():
			for batch in valid_loader:
				nctid_lst, label_vec, smiles_lst2, icdcode_lst3, criteria_lst = batch
				label_vec.to(self.device)
				output = self.student.forward(smiles_lst2, icdcode_lst3, criteria_lst).view(-1)
				loss = self.student.loss(output, label_vec.float())
				total_loss += loss.item()
				count += 1
		self.student.train()
		return total_loss / count if count > 0 else float("inf")
	
	def validate_teacher(self, valid_loader):
		self.teacher.eval()
		total_loss, count = 0, 0
		with torch.no_grad():
			for batch in valid_loader:
				nctid_lst, label_vec, smiles_lst2, icdcode_lst3, criteria_lst = batch
				label_vec.to(self.device)
				output = self.teacher.forward(smiles_lst2, icdcode_lst3, criteria_lst).view(-1)
				loss = self.teacher.loss(output, label_vec.float())
				total_loss += loss.item()
				count += 1
		self.teacher.train()
		return total_loss / count if count > 0 else float("inf")
	
	def validate(self, valid_loader):
		return self.validate_student(valid_loader), self.validate_teacher(valid_loader)
	
	def learn(self, train_loader, valid_loader, test_loader, path, num_epochs=10, lr=1e-3, weight_decay=0):
		# we override the native .learn training loop for more custom control
		student_opt = torch.optim.Adam(self.student.parameters(), lr=lr, weight_decay=weight_decay)
		teacher_opt = torch.optim.Adam(self.teacher.parameters(), lr=lr, weight_decay=weight_decay)

		naive_loss_fn = nn.BCEWithLogitsLoss()
		# we once had self.student.loss, we are trying to avoid overlap between student and teacher as much as possible

		# best_valid_student, best_valid_teacher = float("inf"), float("inf")
		# best_student, best_teacher = None, None

		for epoch in range(num_epochs):
			print(f"Epoch {epoch+1}/{num_epochs}")
			for batch in tqdm(train_loader):
				# unpack the batch
				nctid_lst, label_vec, smiles_lst2, icdcode_lst3, criteria_lst = batch
				label_vec = label_vec.to(self.device).float()

				# full forward passes

				# student
				student_output, student_embeddings = self.student.forward(smiles_lst2, icdcode_lst3, criteria_lst, return_embeddings=True)
				student_output = student_output.view(-1)
				loss_student = naive_loss_fn(student_output, label_vec)
				# (batch_size, 1) -> (batch_size,) for binary classification. .view(-1) also used in Interaction
				
				# teacher
				teacher_output, teacher_embeddings = self.teacher.forward(smiles_lst2, icdcode_lst3, criteria_lst, return_embeddings=True)
				teacher_output = teacher_output.view(-1)
				loss_teacher_BCE = naive_loss_fn(teacher_output, label_vec)

				# get encoders and compute gradients

				# student

				# def forward(self, smiles_lst2, icdcode_lst3, criteria_lst, return_attention_matrix = False, return_embeddings=False):
				
				# encoder_student = self.get_encoder_embedding(self.student, smiles_lst2, icdcode_lst3, criteria_lst)
				# encoder_student.requires_grad_(True)
				grad_student = torch.autograd.grad(
					outputs=student_output,
					inputs=student_embeddings,
					grad_outputs=torch.ones_like(student_output),
					create_graph=True,
					allow_unused=True
				)[0]
				if grad_student is None:
					print("We have no gradient, kiddo!")
					grad_student = torch.zeros_like(student_embeddings)
				# print(grad_student)

				# teacher
				# encoder_teacher = self.get_encoder_embedding(self.teacher, smiles_lst2, icdcode_lst3, criteria_lst)
				# encoder_teacher.requires_grad_(True)
				grad_teacher = torch.autograd.grad(
					outputs=teacher_output,
					inputs=teacher_embeddings,
					grad_outputs=torch.ones_like(teacher_output),
					create_graph=True,
					allow_unused=True
				)[0]
				if grad_teacher is None:
					print("We have no gradient, teach!")
					grad_teacher = torch.zeros_like(teacher_embeddings)
				# print(grad_teacher)
				
				# # normalize gradients over batch
				# norm_grad_student = grad_student / (grad_student.abs().max(dim=1, keepdim=True)[0] + 1e-8) # hopefully 1e-8 small enough
				# norm_grad_teacher = grad_teacher / (grad_teacher.abs().max(dim=1, keepdim=True)[0] + 1e-8)

				# compute gradient penalty for teacher,
				# incentivizing teacher's gradient to move opposite to student
				# we find the cosine similarity between student and teacher
				# we square because we want to encourage orthogonal exploration
				loss_grad = (F.cosine_similarity(grad_student, grad_teacher, dim=1).mean()) ** 4
				# grad_teacher_detached = grad_teacher.detach().numpy()
				# loss_grad = (-grad_student.T @ grad_teacher) / np.linalg.norm(grad_teacher_detached, ord=2)
				# print(loss_teacher_BCE)
				# print(grad_student.shape)
				# print(grad_teacher.shape)
				# print(grad_student.T.shape)
				# print((grad_student.T @ grad_teacher).shape)
				print(loss_grad)
				print(loss_teacher_BCE)

				loss_teacher = self._lambda * loss_teacher_BCE + (1 - self._lambda) * loss_grad
				# important note: in the encoders, we aren't quite passing back BCE loss,
				# but rather lambda * BCE loss, which should be ok unless lambda really small (in which case we could increase lr to compensate)

				teacher_opt.zero_grad()
				print(f'loss_teacher : {loss_teacher}')
				loss_teacher.backward(retain_graph=True) # HUGE issues here, but we're finally beginning to succeed in debugging
				# the issue seems to have been state management between student and teacher, not the core math
				# we liberate our memory, by not calling retain_graph, since the batch is over
				teacher_opt.step()

				# backprop + updates
				student_opt.zero_grad()
				print(f'loss_student: {loss_student}')
				loss_student.backward()
				# we preserve graph here to enable gradient computations on existing graph for the teacher
				student_opt.step()
			
			# always save results at end of epoch
			torch.save(self.student, f"{path}_student_{epoch+1}.ckpt")
			torch.save(self.teacher, f"{path}_teacher_{epoch+1}.ckpt")

			student_valid_loss, teacher_valid_loss = self.validate(valid_loader)

			# dynamically update lambda
			self._lambda += self.d

			print(f"Epoch {epoch+1}: Student Loss = {loss_student.item():.4f}")
			print(f"Epoch {epoch+1}: Teacher Loss = {loss_teacher.item():.4f}")
			print(f"Epoch {epoch+1}: Teacher Naive Loss = {loss_teacher_BCE.item():.4f}")
			print(f"Epoch {epoch+1}: Gradient Penalty = {loss_grad.item():.4f}")
			print(f"Epoch {epoch+1}: Student Validation Loss = {student_valid_loss:.4f}")
			print(f"Epoch {epoch+1}: Teacher Validation Loss = {teacher_valid_loss:.4f}")
		
		# end of a run

		# replace with best versions found during validation
		# self.student = best_student
		# self.teacher = best_teacher

		# then evaluate on test set
		# print(f"Student metrics")
		# student_test_metrics = self.student_test(test_loader)
		# print(f"Teacher metrics")
		# teacher_test_metrics = self.teacher_test(test_loader)
		# student_test_metrics, teacher_test_metrics = 0, 0

		
		return self.student, self.teacher



class Only_Molecule(Interaction):

	def __init__(self, molecule_encoder, disease_encoder, protocol_encoder, 
					global_embed_size, 
					highway_num_layer,
					prefix_name, 
					epoch = 20,
					lr = 3e-4, 
					weight_decay = 0):
		super(Only_Molecule, self).__init__(molecule_encoder=molecule_encoder, 
											disease_encoder=disease_encoder, 
											protocol_encoder=protocol_encoder, 
											global_embed_size = global_embed_size, 
											highway_num_layer = highway_num_layer,
											prefix_name = prefix_name, 
											epoch = epoch,
											lr = lr, 
											weight_decay = weight_decay,)
		self.molecule2out = nn.Linear(self.global_embed_size,1)


	def forward(self, smiles_lst2, icdcode_lst3, criteria_lst):
		molecule_embed = self.molecule_encoder.forward_smiles_lst_lst(smiles_lst2)
		return self.molecule2out(molecule_embed)

class Only_Disease(Only_Molecule):

	def __init__(self, molecule_encoder, disease_encoder, protocol_encoder, 
					global_embed_size, 
					highway_num_layer,
					prefix_name, 
					epoch = 20,
					lr = 3e-4, 
					weight_decay = 0):
		super(Only_Disease, self).__init__(molecule_encoder = molecule_encoder, 
											disease_encoder=disease_encoder, 
											protocol_encoder=protocol_encoder, 
											global_embed_size = global_embed_size, 
											highway_num_layer = highway_num_layer,
											prefix_name = prefix_name, 
											epoch = epoch,
											lr = lr, 
											weight_decay = weight_decay,)
		self.disease2out = self.molecule2out 


	def forward(self, smiles_lst2, icdcode_lst3, criteria_lst):
		icd_embed = self.disease_encoder.forward_code_lst3(icdcode_lst3)
		return self.disease2out(icd_embed)
			


def dataloader2Xy(nctid_lst, label_vec, smiles_lst2, icdcode_lst3, criteria_lst, global_icd):
	## label_vec: (n,)
	y = label_vec 

	num_icd = len(global_icd)
	from HINT.utils import smiles_lst2fp 
	fp_lst = [smiles_lst2fp(smiles_lst).reshape(1,-1) for smiles_lst in smiles_lst2]
	fp_mat = np.concatenate(fp_lst, 0)
	# fp_mat = torch.from_numpy(fp_mat)  ### (n,2048)

	icdcode_lst = []
	for lst2 in icdcode_lst3:
		lst = list(reduce(lambda x,y:x+y, lst2))
		lst = [i.split('.')[0] for i in lst]
		lst = set(lst)	
		icd_feature = np.zeros((1,num_icd), np.int32)
		for ele in lst:
			if ele in global_icd:
				idx = global_icd.index(ele)
				icd_feature[0,idx] = 1 
		icdcode_lst.append(icd_feature)
	icdcode_mat = np.concatenate(icdcode_lst, 0)
	X = np.concatenate([fp_mat, icdcode_mat], 1)
	X = torch.from_numpy(X)
	X = X.float()
	# icdcode_mat = torch.from_numpy(icdcode_mat) 

	# X = torch.cat([fp_mat, icdcode_mat], 1)
	return X, y 


class FFNN(nn.Sequential):
	def __init__(self, molecule_dim, diseasecode_dim, 
					global_icd, 
					protocol_dim = 0,
					prefix_name = 'FFNN', 
					epoch = 10,
					lr = 3e-4, 
					weight_decay = 0, 
					):
		super(FFNN, self).__init__()
		self.molecule_dim = molecule_dim 
		self.diseasecode_dim = diseasecode_dim 
		self.protocol_dim = protocol_dim 
		self.prefix_name = prefix_name 
		self.epoch = epoch 
		self.lr = lr 
		self.weight_decay = weight_decay 
		self.global_icd = global_icd 
		self.num_icd = len(global_icd)

		self.fc_dims = [self.molecule_dim + self.diseasecode_dim + self.protocol_dim, 2000, 1000, 200, 50, 1]
		self.fc_layers = nn.ModuleList([nn.Linear(v,self.fc_dims[i+1]) for i,v in enumerate(self.fc_dims[:-1])])
		self.loss = nn.BCEWithLogitsLoss()
		self.save_name = prefix_name 

	def forward(self, X):
		for i in range(len(self.fc_layers) - 1):
			fc_layer = self.fc_layers[i]
			X = fc_layer(X)
		last_layer = self.fc_layers[-1]
		pred = F.sigmoid(last_layer(X))
		return pred 


		

	def learn(self, train_loader, valid_loader, test_loader):
		opt = torch.optim.Adam(self.parameters(), lr = self.lr, weight_decay = self.weight_decay)
		train_loss_record = [] 
		valid_loss = self.test(valid_loader, return_loss=True)
		valid_loss_record = [valid_loss]
		best_valid_loss = valid_loss
		best_model = deepcopy(self)

		for ep in tqdm(range(self.epoch)):
			for nctid_lst, label_vec, smiles_lst2, icdcode_lst3, criteria_lst in train_loader:
				X, _ = dataloader2Xy(nctid_lst, label_vec, smiles_lst2, icdcode_lst3, criteria_lst, self.global_icd)
				output = self.forward(X).view(-1)  #### 32, 1 -> 32, ||  label_vec 32,
				loss = self.loss(output, label_vec.float())
				train_loss_record.append(loss.item())
				opt.zero_grad() 
				loss.backward() 
				opt.step()
			valid_loss = self.test(valid_loader, return_loss=True)
			valid_loss_record.append(valid_loss)
			if valid_loss < best_valid_loss:
				best_valid_loss = valid_loss 
				best_model = deepcopy(self)

		self.plot_learning_curve(train_loss_record, valid_loss_record)
		self = deepcopy(best_model)
		auc_score, f1score, prauc_score, precision, recall, accuracy, predict_1_ratio, label_1_ratio = self.test(test_loader, return_loss = False, validloader = valid_loader)



	def evaluation(self, predict_all, label_all, threshold = 0.5):
		import pickle, os
		from sklearn.metrics import roc_curve, precision_recall_curve
		with open("predict_label.txt", 'w') as fout:
			for i,j in zip(predict_all, label_all):
				fout.write(str(i)[:4] + '\t' + str(j)[:4]+'\n')
		auc_score = roc_auc_score(label_all, predict_all)
		figure_folder = "figure"
		#### ROC-curve 
		fpr, tpr, thresholds = roc_curve(label_all, predict_all, pos_label=1)
		# roc_curve =plt.figure()
		# plt.plot(fpr,tpr,'-',label=self.save_name + ' ROC Curve ')
		# plt.legend(fontsize = 15)
		#plt.savefig(os.path.join(figure_folder,name+"_roc_curve.png"))
		#### PR-curve
		precision, recall, thresholds = precision_recall_curve(label_all, predict_all)
		# plt.plot(recall,precision, label = self.save_name + ' PR Curve')
		# plt.legend(fontsize = 15)
		# plt.savefig(os.path.join(figure_folder,self.save_name + "_pr_curve.png"))
		label_all = [int(i) for i in label_all]
		float2binary = lambda x:0 if x<threshold else 1
		predict_all = list(map(float2binary, predict_all))
		f1score = f1_score(label_all, predict_all)
		prauc_score = average_precision_score(label_all, predict_all)
		# print(predict_all)
		precision = precision_score(label_all, predict_all)
		recall = recall_score(label_all, predict_all)
		accuracy = accuracy_score(label_all, predict_all)
		predict_1_ratio = sum(predict_all) / len(predict_all)
		label_1_ratio = sum(label_all) / len(label_all)
		return auc_score, f1score, prauc_score, precision, recall, accuracy, predict_1_ratio, label_1_ratio 


	def generate_predict(self, dataloader):
		whole_loss = 0 
		label_all, predict_all = [], []
		for nctid_lst, label_vec, smiles_lst2, icdcode_lst3, criteria_lst in dataloader:
			X, _ = dataloader2Xy(nctid_lst, label_vec, smiles_lst2, icdcode_lst3, criteria_lst, self.global_icd) 
			output = self.forward(X).view(-1)  
			loss = self.loss(output, label_vec.float())
			whole_loss += loss.item()
			predict_all.extend([i.item() for i in torch.sigmoid(output)])
			label_all.extend([i.item() for i in label_vec])

		return whole_loss, predict_all, label_all

	def bootstrap_test(self, dataloader, sample_num = 20):
		# if validloader is not None:
		# 	best_threshold = self.select_threshold_for_binary(validloader)
		self.eval()
		best_threshold = 0.5 
		whole_loss, predict_all, label_all = self.generate_predict(dataloader)
		from HINT.utils import plot_hist
		plt.clf()
		prefix_name = "./figure/" + self.save_name 
		#plot_hist(prefix_name, predict_all, label_all)		
		def bootstrap(length, sample_num):
			idx = [i for i in range(length)]
			from random import choices 
			bootstrap_idx = [choices(idx, k = length) for i in range(sample_num)]
			return bootstrap_idx 
		results_lst = []
		bootstrap_idx_lst = bootstrap(len(predict_all), sample_num = sample_num)
		for bootstrap_idx in bootstrap_idx_lst: 
			bootstrap_label = [label_all[idx] for idx in bootstrap_idx]		
			bootstrap_predict = [predict_all[idx] for idx in bootstrap_idx]
			results = self.evaluation(bootstrap_predict, bootstrap_label, threshold = best_threshold)
			results_lst.append(results)
		self.train() 
		auc = [results[0] for results in results_lst]
		f1score = [results[1] for results in results_lst]
		prauc_score = [results[2] for results in results_lst]
		print("PR-AUC   mean: "+str(np.mean(prauc_score))[:6], "std: "+str(np.std(prauc_score))[:6])
		print("F1       mean: "+str(np.mean(f1score))[:6], "std: "+str(np.std(f1score))[:6])
		print("ROC-AUC  mean: "+ str(np.mean(auc))[:6], "std: " + str(np.std(auc))[:6])


	def test(self, dataloader, return_loss = True, validloader=None):
		# if validloader is not None:
		# 	best_threshold = self.select_threshold_for_binary(validloader)
		self.eval()
		best_threshold = 0.5 
		whole_loss, predict_all, label_all = self.generate_predict(dataloader)
		# from HINT.utils import plot_hist
		# plt.clf()
		# prefix_name = "./figure/" + self.save_name 
		# plot_hist(prefix_name, predict_all, label_all)
		self.train()
		if return_loss:
			return whole_loss
		else:
			print_num = 5
			auc_score, f1score, prauc_score, precision, recall, accuracy, \
			predict_1_ratio, label_1_ratio = self.evaluation(predict_all, label_all, threshold = best_threshold)
			print("ROC AUC: " + str(auc_score)[:print_num] + "\nF1: " + str(f1score)[:print_num] \
				 + "\nPR-AUC: " + str(prauc_score)[:print_num] \
				 + "\nPrecision: " + str(precision)[:print_num] \
				 + "\nrecall: "+str(recall)[:print_num] + "\naccuracy: "+str(accuracy)[:print_num] \
				 + "\npredict 1 ratio: " + str(predict_1_ratio)[:print_num] \
				 + "\nlabel 1 ratio: " + str(label_1_ratio)[:print_num])
			return auc_score, f1score, prauc_score, precision, recall, accuracy, predict_1_ratio, label_1_ratio 


	def plot_learning_curve(self, train_loss_record, valid_loss_record):
		plt.plot(train_loss_record)
		plt.savefig("./figure/" + self.save_name + '_train_loss.jpg')
		plt.clf() 
		plt.plot(valid_loss_record)
		plt.savefig("./figure/" + self.save_name + '_valid_loss.jpg')
		plt.clf() 




class ADMET(nn.Sequential):
	def __init__(self, mpnn_model, device):
		super(ADMET, self).__init__()
		self.num = 5 
		self.mpnn_model = mpnn_model 
		self.device = device 
		self.mpnn_dim = mpnn_model.mpnn_hidden_size 
		self.admet_model = []
		self.global_embed_size = self.mpnn_dim 
		self.highway_num_layer = 2 
		for i in range(5):
			admet_fc = nn.Linear(self.mpnn_model.mpnn_hidden_size, self.global_embed_size).to(device)
			admet_highway = Highway(self.global_embed_size, self.highway_num_layer).to(device)
			self.admet_model.append(nn.ModuleList([admet_fc, admet_highway]))
		self.admet_model = nn.ModuleList(self.admet_model)

		self.admet_pred = nn.ModuleList([nn.Linear(self.global_embed_size,1).to(device) for i in range(5)])
		self.f = F.relu 

		self.device = device 
		self = self.to(device)

	def feed_lst_of_module(self, input_feature, lst_of_module):
		x = input_feature
		for single_module in lst_of_module:
			x = self.f(single_module(x))
		return x 

	def forward(self, smiles_lst, idx):
		assert idx in list(range(5))
		'''
			xxxxxxxxxxxx
		'''
		embeds = self.mpnn_model.forward_smiles_lst_lst(smiles_lst)
		embeds = self.feed_lst_of_module(embeds, self.admet_model[idx]) 
		output = self.admet_pred[idx](embeds)
		return output 

	def test(self, valid_loader):
		pass 


	def learn(self, train_loader, valid_loader, idx):
		opt = torch.optim.Adam(self.parameters(), lr = self.lr, weight_decay = self.weight_decay)
		train_loss_record = [] 
		valid_loss = self.test(valid_loader, return_loss=True)
		valid_loss_record = [valid_loss]
		best_valid_loss = valid_loss
		best_model = deepcopy(self)

		for ep in tqdm(range(self.epoch)):
			for smiles_lst in train_loader:
				output = self.forward(smiles_lst).view(-1)  #### 32, 1 -> 32, ||  label_vec 32,
				loss = self.loss(output, label_vec.float())
				train_loss_record.append(loss.item())
				opt.zero_grad() 
				loss.backward() 
				opt.step()
			valid_loss = self.test(valid_loader, return_loss=True)
			valid_loss_record.append(valid_loss)
			if valid_loss < best_valid_loss:
				best_valid_loss = valid_loss 
				best_model = deepcopy(self)

		self = deepcopy(best_model)