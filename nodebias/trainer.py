import pickle
from ._utils import *

class Trainer:
    def __init__(self, dataloader, learner, optimisers, key=None):
        self.key = get_new_key(key)

        self.dataloader = dataloader
        self.learner = learner
        self.opt_node, self.opt_ctx = optimisers

        self.opt_node_state = self.opt_node.init(eqx.filter(self.learner.neuralode, eqx.is_array))
        self.opt_ctx_state = self.opt_ctx.init(self.learner.contexts)

        self.losses_node = []
        self.losses_ctx = []
        self.nb_steps_node = []
        self.nb_steps_ctx = []

    def train(self, nb_epochs, update_context_every=1, print_error_every=100, save_path=False, key=None):
        # key = key if key is not None else self.key

        opt_state_node = self.opt_node_state
        opt_state_ctx = self.opt_ctx_state

        loss_fn = self.learner.loss_fn

        node = self.learner.neuralode
        contexts = self.learner.contexts

        @eqx.filter_jit
        def train_step_node(node, contexts, batch, weights, opt_state):
            print('\nCompiling function "train_step" for neural ode ...\n')

            (loss, aux_data), grads = eqx.filter_value_and_grad(loss_fn, has_aux=True)(node, contexts, batch, weights)

            updates, opt_state = self.opt_node.update(grads, opt_state)
            node = eqx.apply_updates(node, updates)

            return node, contexts, opt_state, loss, aux_data


        @eqx.filter_jit
        def train_step_ctx(node, contexts, batch, weights, opt_state):
            print('\nCompiling function "train_step" for context ...\n')

            loss_fn_ = lambda contexts, node, batch, weights: loss_fn(node, contexts, batch, weights)

            (loss, aux_data), grads = eqx.filter_value_and_grad(loss_fn_, has_aux=True)(contexts, node, batch, weights)

            updates, opt_state = self.opt_ctx.update(grads, opt_state)
            contexts = eqx.apply_updates(contexts, updates)

            return node, contexts, opt_state, loss, aux_data


        nb_train_steps_per_epoch = int(np.ceil(self.dataloader.nb_trajs_per_env / self.dataloader.batch_size))
        total_steps = nb_epochs * nb_train_steps_per_epoch

        assert update_context_every <= nb_train_steps_per_epoch, "update_context_every must be smaller than nb_train_steps_per_epoch"

        print(f"\n\n=== Beginning training neural ODE ... ===")
        print(f"    Number of examples in a batch: {self.dataloader.batch_size}")
        print(f"    Number of train steps per epoch: {nb_train_steps_per_epoch}")
        print(f"    Number of training epochs: {nb_epochs}")
        print(f"    Total number of training steps: {total_steps}")

        start_time = time.time()

        losses_node = []
        losses_ctx = []
        nb_steps_node = []
        nb_steps_ctx = []

        weights = jnp.ones(self.learner.nb_envs) / self.learner.nb_envs

        for epoch in range(nb_epochs):
            nb_batches_node = 0
            nb_batches_ctx = 0
            loss_sum_node = jnp.zeros(1)
            loss_sum_ctx = jnp.zeros(1)
            nb_steps_eph_node = 0
            nb_steps_eph_ctx = 0

            for i, batch in enumerate(self.dataloader):

                node, contexts, opt_state_node, loss_node, (nb_steps_node_, term1, term2) = train_step_node(node, contexts, batch, weights, opt_state_node)

                term1 = term1 + 1e-8
                weights = term1 / jnp.sum(term1)

                loss_sum_node += jnp.array([loss_node])
                nb_steps_eph_node += nb_steps_node_

                nb_batches_node += 1

                if i%update_context_every==0:
                    node, contexts, opt_state_ctx, loss_ctx, (nb_steps_ctx_, term1, term2) = train_step_ctx(node, contexts, batch, weights, opt_state_ctx)

                    term1 = term1 + 1e-8
                    weights = term1 / jnp.sum(term1)

                    loss_sum_ctx += jnp.array([loss_ctx])
                    nb_steps_eph_ctx += nb_steps_ctx_

                    nb_batches_ctx += 1

            loss_epoch_node = loss_sum_node/nb_batches_node
            loss_epoch_ctx = loss_sum_ctx/nb_batches_ctx

            losses_node.append(loss_epoch_node)
            losses_ctx.append(loss_epoch_ctx)
            nb_steps_node.append(nb_steps_eph_node)
            nb_steps_ctx.append(nb_steps_eph_ctx)

            if epoch%print_error_every==0 or epoch<=3 or epoch==nb_epochs-1:
                print(f"    Epoch: {epoch:-5d}      LossNeuralODE: {loss_epoch_node[0]:-.8f}     LossContext: {loss_epoch_ctx[0]:-.8f}", flush=True)

        wall_time = time.time() - start_time
        time_in_hmsecs = seconds_to_hours(wall_time)
        print("\nTotal gradient descent training time: %d hours %d mins %d secs" %time_in_hmsecs)

        self.losses_node.append(jnp.vstack(losses_node))
        self.losses_ctx.append(jnp.vstack(losses_ctx))
        self.nb_steps_node.append(jnp.array(nb_steps_node))
        self.nb_steps_ctx.append(jnp.array(nb_steps_ctx))

        self.opt_node_state = opt_state_node
        self.opt_ctx_state = opt_state_ctx

        self.learner.neuralode = node
        self.learner.contexts = contexts

        ## Save the model and results
        if save_path:
            self.save_trainer(save_path)


    def save_trainer(self, path):
        print(f"\nSaving model and results into {path} folder ...\n")

        np.savez(path+"train_histories.npz", 
                 losses_node=jnp.vstack(self.losses_node), 
                 losses_cont=jnp.vstack(self.losses_ctx), 
                 nb_steps_node=jnp.array(self.nb_steps_node), 
                 nb_steps_cont=jnp.array(self.nb_steps_ctx))

        pickle.dump(self.opt_state_node, open(path+"/opt_state_node.pkl", "wb"))
        pickle.dump(self.opt_state_ctx, open(path+"/opt_state_ctx.pkl", "wb"))

        self.learner.save_learner(path)


    def load_trainer(self, path):
        print(f"\nNo training, loading model and results from {path} folder ...\n")

        histories = np.load(path+"train_histories.npz")
        self.losses_node = histories['losses_node']
        self.losses_cont = histories['losses_cont']
        self.nb_steps_node = histories['nb_steps_node']
        self.nb_steps_cont = histories['nb_steps_cont']

        self.opt_state_node = pickle.load(path+"/opt_state_node.pkl")
        self.opt_state_ctx = pickle.load(path+"/opt_state_ctx.pkl")

        self.learner.load_learner(path)
