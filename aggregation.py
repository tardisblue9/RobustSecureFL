import torch
import models
from torch.nn.utils import vector_to_parameters, parameters_to_vector
import numpy as np
import random
from copy import deepcopy
from torch.nn import functional as F

from geom_median.torch import compute_geometric_median

class Aggregation():
    def __init__(self, agent_data_sizes, n_params, poisoned_val_loader, args, writer):
        self.agent_data_sizes = agent_data_sizes
        self.args = args
        self.writer = writer
        self.server_lr = args.server_lr
        self.n_params = n_params
        self.poisoned_val_loader = poisoned_val_loader
        self.cum_net_mov = 0
        
         
    def aggregate_updates(self, global_model, agent_updates_dict, cur_round):
        # adjust LR if robust LR is selected
        lr_vector = torch.Tensor([self.server_lr]*self.n_params).to(self.args.device)
        if self.args.robustLR_threshold > 0:
            lr_vector = self.compute_robustLR(agent_updates_dict)
        
        
        aggregated_updates = 0
        if self.args.aggr=='avg':          
            aggregated_updates = self.agg_avg(agent_updates_dict)
        elif self.args.aggr=='quantize_avg':
            aggregated_updates = self.quantize_avg(agent_updates_dict)
        elif self.args.aggr=='comed':
            aggregated_updates = self.agg_comed(agent_updates_dict)
        elif self.args.aggr == 'sign':
            aggregated_updates = self.agg_sign(agent_updates_dict)
        elif self.args.aggr == 'geomed':
            concat_col_vectors = self.dummy(agent_updates_dict)
            aggregated_updates = self.geomed(concat_col_vectors)
        elif self.args.aggr == 'poison_sign_flip':
            print("====== For model poisoning - sign flip attack is launched! =====")
            concat_col_vectors = self.poison_sign_flip(agent_updates_dict, 3)
            if self.args.defence == None:
                aggregated_updates = torch.mean(concat_col_vectors, dim=1)
            elif self.args.defence == 'coord_median':
                aggregated_updates = torch.median(concat_col_vectors, dim=1).values
            elif self.args.defence == 'Geo_median':
                aggregated_updates = self.geomed(concat_col_vectors)
        elif self.args.aggr == 'poison_additive_noise':
            print("====== For model poisoning - additive_noise attack is launched! =====")
            concat_col_vectors = self.poison_additive_noise(agent_updates_dict, 1)
            if self.args.defence == None:
                aggregated_updates = torch.mean(concat_col_vectors, dim=1)
            elif self.args.defence == 'coord_median':
                print("=== Protected by Coordinate median ===")
                aggregated_updates = torch.median(concat_col_vectors, dim=1).values
            elif self.args.defence == 'Geo_median':
                print("=== Protected by Geometric median ===")
                aggregated_updates = self.geomed(concat_col_vectors)
        elif self.args.aggr == 'poison_scale':
            print("====== For model poisoning - scaling attack is launched! =====")
            concat_col_vectors = self.poison_scale(agent_updates_dict, 1)
            if self.args.defence == None:
                aggregated_updates = torch.mean(concat_col_vectors, dim=1)
            elif self.args.defence == 'coord_median':
                print("=== Protected by Coordinate median ===")
                aggregated_updates = torch.median(concat_col_vectors, dim=1).values
            elif self.args.defence == 'Geo_median':
                print("=== Protected by Geometric median ===")
                aggregated_updates = self.geomed(concat_col_vectors)
        elif self.args.aggr == 'poison_minmax':
            print("====== For model poisoning - min-max attack is launched! =====")
            concat_col_vectors = self.poison_minmax(agent_updates_dict, 3)
            if self.args.defence == None:
                aggregated_updates = torch.mean(concat_col_vectors, dim=1)
            elif self.args.defence == 'coord_median':
                print("=== Protected by Coordinate median ===")
                aggregated_updates = torch.median(concat_col_vectors, dim=1).values
            elif self.args.defence == 'Geo_median':
                print("=== Protected by Geometric median ===")
                aggregated_updates = self.geomed(concat_col_vectors)

            
        if self.args.noise > 0:
            aggregated_updates.add_(torch.normal(mean=0, std=self.args.noise*self.args.clip, size=(self.n_params,)).to(self.args.device))
        
                
        cur_global_params = parameters_to_vector(global_model.parameters())
        new_global_params =  (cur_global_params + lr_vector*aggregated_updates).float() 
        vector_to_parameters(new_global_params, global_model.parameters())
        
        # some plotting stuff if desired
        # self.plot_sign_agreement(lr_vector, cur_global_params, new_global_params, cur_round)
        # self.plot_norms(agent_updates_dict, cur_round)
        return           
     
    
    def compute_robustLR(self, agent_updates_dict):
        agent_updates_sign = [torch.sign(update) for update in agent_updates_dict.values()]  
        sm_of_signs = torch.abs(sum(agent_updates_sign))
        
        sm_of_signs[sm_of_signs < self.args.robustLR_threshold] = -self.server_lr
        sm_of_signs[sm_of_signs >= self.args.robustLR_threshold] = self.server_lr                                            
        return sm_of_signs.to(self.args.device)
        
            
    def agg_avg(self, agent_updates_dict):
        """ classic fed avg """
        sm_updates, total_data = 0, 0
        for _id, update in agent_updates_dict.items():
            n_agent_data = self.agent_data_sizes[_id]
            sm_updates +=  n_agent_data * update
            total_data += n_agent_data  
        return  sm_updates / total_data
    
    def quantize_avg(self, agent_updates_dict):
        """
            Simulate the loss caused by the quantization
        """
        agent_updates_col_vector = [update.view(-1, 1) for update in agent_updates_dict.values()]
        concat_col_vectors = torch.cat(agent_updates_col_vector, dim=1)
        update = torch.mean(concat_col_vectors, dim=1)
        field_q = 10**4
        quantized_update = torch.round(update*field_q)/field_q
        return quantized_update

    def agg_comed(self, agent_updates_dict):
        agent_updates_col_vector = [update.view(-1, 1) for update in agent_updates_dict.values()]
        concat_col_vectors = torch.cat(agent_updates_col_vector, dim=1)
        return torch.median(concat_col_vectors, dim=1).values
    
    def agg_sign(self, agent_updates_dict):
        """ aggregated majority sign update """
        agent_updates_sign = [torch.sign(update) for update in agent_updates_dict.values()]
        sm_signs = torch.sign(sum(agent_updates_sign))
        return torch.sign(sm_signs)

    def dummy(self, agent_updates_dict):
        agent_updates_col_vector = [update.view(-1, 1) for update in agent_updates_dict.values()]
        concat_col_vectors = torch.cat(agent_updates_col_vector, dim=1)
        return concat_col_vectors

    def geomed(self, concat_col_vectors):
        agent_updates_col_vector = [update for update in torch.transpose(concat_col_vectors, 0, 1)]
        geo_median = compute_geometric_median(agent_updates_col_vector, weights=None).median
        geo_median = geo_median.view(1, -1)
        return torch.squeeze(geo_median)

    def poison_sign_flip(self, agent_updates_dict, num_corrupt=0):
        """
            sign flip attack
            num_corrupt : number of sign-flipped users
        """
        agent_updates_col_vector = [update.view(-1, 1) for update in agent_updates_dict.values()]
        concat_col_vectors = torch.cat(agent_updates_col_vector, dim=1)
        concat_col_vectors[:, :num_corrupt] += - concat_col_vectors[:, :num_corrupt]  # flip sign
        return concat_col_vectors

    def poison_additive_noise(self, agent_updates_dict, num_corrupt=0):
        """
            Additive Noise Attack
            num_corrupt : number of malicious users
        """
        agent_updates_col_vector = [update.view(-1, 1) for update in agent_updates_dict.values()]
        concat_col_vectors = torch.cat(agent_updates_col_vector, dim=1)
        random_noise_vectors = torch.randn(concat_col_vectors.shape[0], num_corrupt).to(self.args.device)
        c = 1 # std
        concat_col_vectors[:, :num_corrupt] += c * random_noise_vectors  # additive
        # concat_col_vectors[:, :num_corrupt] = c * random_noise_vectors # overwrite
        return concat_col_vectors

    def poison_scale(self, agent_updates_dict, num_corrupt=0):
        """
            Scaling Attack
        """
        agent_updates_col_vector = [update.view(-1, 1) for update in agent_updates_dict.values()]
        concat_col_vectors = torch.cat(agent_updates_col_vector, dim=1)
        c = 10 # std
        concat_col_vectors[:, :num_corrupt] += c * concat_col_vectors[:, :num_corrupt]  # amplify
        return concat_col_vectors

    def poison_minmax(self, agent_updates_dict, num_corrupt=0):
        """
            min max attack, num_corrupt should be 1 in this case, just copy malicious behavior if num_corrupt is greater than one
            v_avg + \gamma*v_p -v_i
        """
        agent_updates_col_vector = [update.view(-1, 1) for update in agent_updates_dict.values()]
        concat_col_vectors = torch.cat(agent_updates_col_vector, dim=1)
        datadim, N = concat_col_vectors.shape
        # print(datadim, N)

        benign_vector = torch.mean(concat_col_vectors[:, num_corrupt:], dim=1)
        v2 = - benign_vector/torch.norm(benign_vector) # perturbation vector: Inverse unit vector
        # v2 = - torch.std(concat_col_vectors[:, num_corrupt:], dim=1, keepdim=False) # perturbation vector: Inverse standard deviation
        dists = []
        for i in range(num_corrupt, N): # go over benign vectors
            for j in range(num_corrupt, N):
                dist = torch.norm(concat_col_vectors[:, i] - concat_col_vectors[:, j])
                dists.append(dist)
        dist = torch.max(torch.tensor(dists))
        v1 = benign_vector - concat_col_vectors[:, random.randint(num_corrupt, N-1)] # any benign vector
        v1, v2 = v1.unsqueeze(1), v2.unsqueeze(1) # convert to (d,1) column vector
        a = torch.matmul(v2.T, v2)
        b = torch.matmul(v2.T, v1) + torch.matmul(v1.T, v2)
        c = - dist**2 + torch.matmul(v1.T, v1)
        delta = b**2 - 4*a*c
        # x1 = (-b - torch.sqrt(delta))/(2*a)
        x2 = (-b + torch.sqrt(delta))/(2*a)
        m_vector = benign_vector + torch.squeeze(torch.sqrt(x2) * v2)

        for i in range(num_corrupt):
            concat_col_vectors[:, i] = m_vector
        return concat_col_vectors

    def poison_minsum(self, agent_updates_dict, num_corrupt=0):
        """
            min sum attack, num_corrupt should be 1 in this case, just copy malicious behavior if num_corrupt is greater than one
        """
        agent_updates_col_vector = [update.view(-1, 1) for update in agent_updates_dict.values()]
        concat_col_vectors = torch.cat(agent_updates_col_vector, dim=1)
        c = 10 # std
        concat_col_vectors[:, :num_corrupt] += c * concat_col_vectors[:, :num_corrupt]  # amplify
        return concat_col_vectors

    def clip_updates(self, agent_updates_dict):
        for update in agent_updates_dict.values():
            l2_update = torch.norm(update, p=2) 
            update.div_(max(1, l2_update/self.args.clip))
        return
                  
    def plot_norms(self, agent_updates_dict, cur_round, norm=2):
        """ Plotting average norm information for honest/corrupt updates """
        honest_updates, corrupt_updates = [], []
        for key in agent_updates_dict.keys():
            if key < self.args.num_corrupt:
                corrupt_updates.append(agent_updates_dict[key])
            else:
                honest_updates.append(agent_updates_dict[key])
                              
        l2_honest_updates = [torch.norm(update, p=norm) for update in honest_updates]
        avg_l2_honest_updates = sum(l2_honest_updates) / len(l2_honest_updates)
        self.writer.add_scalar(f'Norms/Avg_Honest_L{norm}', avg_l2_honest_updates, cur_round)
        
        if len(corrupt_updates) > 0:
            l2_corrupt_updates = [torch.norm(update, p=norm) for update in corrupt_updates]
            avg_l2_corrupt_updates = sum(l2_corrupt_updates) / len(l2_corrupt_updates)
            self.writer.add_scalar(f'Norms/Avg_Corrupt_L{norm}', avg_l2_corrupt_updates, cur_round) 
        return
        
    def comp_diag_fisher(self, model_params, data_loader, adv=True):

        model = models.get_model(self.args.data)
        vector_to_parameters(model_params, model.parameters())
        params = {n: p for n, p in model.named_parameters() if p.requires_grad}
        precision_matrices = {}
        for n, p in deepcopy(params).items():
            p.data.zero_()
            precision_matrices[n] = p.data
            
        model.eval()
        for _, (inputs, labels) in enumerate(data_loader):
            model.zero_grad()
            inputs, labels = inputs.to(device=self.args.device, non_blocking=True),\
                                    labels.to(device=self.args.device, non_blocking=True).view(-1, 1)
            if not adv:
                labels.fill_(self.args.base_class)
                
            outputs = model(inputs)
            log_all_probs = F.log_softmax(outputs, dim=1)
            target_log_probs = outputs.gather(1, labels)
            batch_target_log_probs = target_log_probs.sum()
            batch_target_log_probs.backward()
            
            for n, p in model.named_parameters():
                precision_matrices[n].data += (p.grad.data ** 2) / len(data_loader.dataset)
                
        return parameters_to_vector(precision_matrices.values()).detach()

        
    def plot_sign_agreement(self, robustLR, cur_global_params, new_global_params, cur_round):
        """ Getting sign agreement of updates between honest and corrupt agents """
        # total update for this round
        update = new_global_params - cur_global_params
        
        # compute FIM to quantify these parameters: (i) parameters which induces adversarial mapping on trojaned, (ii) parameters which induces correct mapping on trojaned
        fisher_adv = self.comp_diag_fisher(cur_global_params, self.poisoned_val_loader)
        fisher_hon = self.comp_diag_fisher(cur_global_params, self.poisoned_val_loader, adv=False)
        _, adv_idxs = fisher_adv.sort()
        _, hon_idxs = fisher_hon.sort()
        
        # get most important n_idxs params
        n_idxs = self.args.top_frac #math.floor(self.n_params*self.args.top_frac)
        adv_top_idxs = adv_idxs[-n_idxs:].cpu().detach().numpy()
        hon_top_idxs = hon_idxs[-n_idxs:].cpu().detach().numpy()
        
        # minimized and maximized indexes
        min_idxs = (robustLR == -self.args.server_lr).nonzero().cpu().detach().numpy()
        max_idxs = (robustLR == self.args.server_lr).nonzero().cpu().detach().numpy()
        
        # get minimized and maximized idxs for adversary and honest
        max_adv_idxs = np.intersect1d(adv_top_idxs, max_idxs)
        max_hon_idxs = np.intersect1d(hon_top_idxs, max_idxs)
        min_adv_idxs = np.intersect1d(adv_top_idxs, min_idxs)
        min_hon_idxs = np.intersect1d(hon_top_idxs, min_idxs)
       
        # get differences
        max_adv_only_idxs = np.setdiff1d(max_adv_idxs, max_hon_idxs)
        max_hon_only_idxs = np.setdiff1d(max_hon_idxs, max_adv_idxs)
        min_adv_only_idxs = np.setdiff1d(min_adv_idxs, min_hon_idxs)
        min_hon_only_idxs = np.setdiff1d(min_hon_idxs, min_adv_idxs)
        
        # get actual update values and compute L2 norm
        max_adv_only_upd = update[max_adv_only_idxs] # S1
        max_hon_only_upd = update[max_hon_only_idxs] # S2
        
        min_adv_only_upd = update[min_adv_only_idxs] # S3
        min_hon_only_upd = update[min_hon_only_idxs] # S4


        #log l2 of updates
        max_adv_only_upd_l2 = torch.norm(max_adv_only_upd).item()
        max_hon_only_upd_l2 = torch.norm(max_hon_only_upd).item()
        min_adv_only_upd_l2 = torch.norm(min_adv_only_upd).item()
        min_hon_only_upd_l2 = torch.norm(min_hon_only_upd).item()
       
        self.writer.add_scalar(f'Sign/Hon_Maxim_L2', max_hon_only_upd_l2, cur_round)
        self.writer.add_scalar(f'Sign/Adv_Maxim_L2', max_adv_only_upd_l2, cur_round)
        self.writer.add_scalar(f'Sign/Adv_Minim_L2', min_adv_only_upd_l2, cur_round)
        self.writer.add_scalar(f'Sign/Hon_Minim_L2', min_hon_only_upd_l2, cur_round)
        
        
        net_adv =  max_adv_only_upd_l2 - min_adv_only_upd_l2
        net_hon =  max_hon_only_upd_l2 - min_hon_only_upd_l2
        self.writer.add_scalar(f'Sign/Adv_Net_L2', net_adv, cur_round)
        self.writer.add_scalar(f'Sign/Hon_Net_L2', net_hon, cur_round)
        
        self.cum_net_mov += (net_hon - net_adv)
        self.writer.add_scalar(f'Sign/Model_Net_L2_Cumulative', self.cum_net_mov, cur_round)
        return