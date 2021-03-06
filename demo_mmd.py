
import torch
import torch.nn as nn
import torch.backends.cudnn as cudnn
import torch.utils.data
import torchvision.utils as vutils
from torch.autograd import Variable
import torch.nn.functional as F

import os, timeit, random
import numpy as np
import argparse

import model.mmd_utils as util
from model.mmd_kernels import mix_rbf_mmd2
from model.mmd_gan import Encoder, Decoder, weights_init, grad_norm
from util.helper import mkdir

os.environ['CUDA_VISIBLE_DEVICES'] = "2"

'''
	NetD is an encoder + decoder
	input: batch_size * nc * image_size * image_size
	f_enc_X: batch_size * k * 1 * 1
	f_dec_X: batch_size * nc * image_size * image_size
'''
class NetD(nn.Module):
	def __init__(self, encoder, decoder):
		super(NetD, self).__init__()
		self.encoder = encoder
		self.decoder = decoder

	def forward(self, input):
		f_enc_X = self.encoder(input)
		f_dec_X = self.decoder(f_enc_X)

		f_enc_X = f_enc_X.view(input.size(0), -1)
		f_dec_X = f_dec_X.view(input.size(0), -1)
		return f_enc_X, f_dec_X

class NetG(nn.Module):
	def __init__(self, decoder):
		super(NetG, self).__init__()
		self.decoder = decoder

	def forward(self, input):
		output = self.decoder(input)
		return output


class ONE_SIDED(nn.Module):
	def __init__(self):
		super(ONE_SIDED, self).__init__()

		main = nn.ReLU()
		self.main = main

	def forward(self, input):
		output = self.main(-input)
		output = -output.mean()
		return output


############### Get argument
parser = argparse.ArgumentParser()
parser = util.get_args(parser)
args = parser.parse_args()
print(args)

if args.experiment is None:
	args.experiment = 'samples'
mkdir(args.experiment)

device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
if torch.cuda.is_available():
	args.cuda = True
	torch.cuda.set_device(args.gpu_device)
	print("Using GPU device", torch.cuda.current_device())
else:
	raise EnvironmentError("GPU device not available!")

args.manual_seed = 1126
np.random.seed(seed=args.manual_seed)
random.seed(args.manual_seed)
torch.manual_seed(args.manual_seed)
torch.cuda.manual_seed(args.manual_seed)
cudnn.benchmark = True


# Get data
trn_dataset = util.get_data(args, train_flag=True)
trn_loader = torch.utils.data.DataLoader(trn_dataset,
										 batch_size=args.batch_size,
										 shuffle=True,
										 num_workers=int(args.workers))


# construct encoder/decoder modules
hidden_dim = args.nz
G_decoder = Decoder(embed_dim=args.nz, output_dim=args.image_size**2)
D_encoder = Encoder(input_dim=args.image_size**2, embed_dim=args.nz)
D_decoder = Decoder(embed_dim=args.nz, output_dim=args.image_size**2)

netG = NetG(G_decoder).to(device)
netD = NetD(D_encoder, D_decoder).to(device)
one_sided = ONE_SIDED().to(device)
netG.apply(weights_init)
netD.apply(weights_init)
one_sided.apply(weights_init)

# sigma for MMD
base = 1.0
sigma_list = [1, 2, 4, 8, 16]
sigma_list = [sigma / base for sigma in sigma_list]

# put variable into cuda device
fixed_noise = torch.cuda.FloatTensor(64, args.nz, 1, 1).normal_(0, 1)
one = torch.cuda.FloatTensor([1])
mone = one * -1
fixed_noise = Variable(fixed_noise, requires_grad=False)

# setup optimizer
optimizerG = torch.optim.RMSprop(netG.parameters(), lr=args.lr)
optimizerD = torch.optim.RMSprop(netD.parameters(), lr=args.lr)

lambda_MMD = 1.0
lambda_AE_X = 8.0
lambda_AE_Y = 8.0
lambda_rg = 16.0

time = timeit.default_timer()
gen_iterations = 0

netG_path = '{0}/netG_iter_50.pth'.format(args.experiment)
if os.path.isfile(netG_path):
	print('Load existing models')
	netG.load_state_dict(torch.load(netG_path))
	y_fixed = netG(fixed_noise)
	y_fixed = y_fixed.view(y_fixed.size(0), args.nc, args.image_size, args.image_size)
	y_fixed.data = y_fixed.data.mul(0.5).add(0.5)
	vutils.save_image(y_fixed.data, '{0}/fake_samples.png'.format(args.experiment))
else:
	for t in range(args.max_iter):
		data_iter = iter(trn_loader)
		i = 0
		while (i < len(trn_loader)):
			# ---------------------------
			#        Optimize over NetD
			# ---------------------------
			for p in netD.parameters():
				p.requires_grad = True

			# if gen_iterations < 25 or gen_iterations % 500 == 0:
			Diters = 1
			Giters = 1

			for j in range(Diters):
				if i == len(trn_loader):
					break

				# clamp parameters of NetD encoder to a cube
				# do not clamp paramters of NetD decoder!!!
				for p in netD.encoder.parameters():
					p.data.clamp_(-0.01, 0.01)

				data = data_iter.next()
				i += 1
				netD.zero_grad()

				x_cpu, _ = data
				x = Variable(x_cpu.cuda())
				batch_size = x.size(0)

				f_enc_X_D, f_dec_X_D = netD(x)

				noise = torch.cuda.FloatTensor(batch_size, args.nz, 1, 1).normal_(0, 1)
				noise = Variable(noise, volatile=True)  # total freeze netG
				y = Variable(netG(noise).data)

				f_enc_Y_D, f_dec_Y_D = netD(y)

				# compute biased MMD2 and use ReLU to prevent negative value
				mmd2_D = mix_rbf_mmd2(f_enc_X_D, f_enc_Y_D, sigma_list)
				mmd2_D = F.relu(mmd2_D)

				# compute rank hinge loss
				#print('f_enc_X_D:', f_enc_X_D.size())
				#print('f_enc_Y_D:', f_enc_Y_D.size())
				one_side_errD = one_sided(f_enc_X_D.mean(0) - f_enc_Y_D.mean(0))

				# compute L2-loss of AE
				L2_AE_X_D = util.match(x.view(batch_size, -1), f_dec_X_D, 'L2')
				L2_AE_Y_D = util.match(y.view(batch_size, -1), f_dec_Y_D, 'L2')

				errD = torch.sqrt(mmd2_D) + lambda_rg * one_side_errD - lambda_AE_X * L2_AE_X_D - lambda_AE_Y * L2_AE_Y_D
				(-errD).backward()
				optimizerD.step()

			# ---------------------------
			#        Optimize over NetG
			# ---------------------------
			for p in netD.parameters():
				p.requires_grad = False

			for j in range(Giters):
				if i == len(trn_loader):
					break

				data = data_iter.next()
				i += 1
				netG.zero_grad()

				x_cpu, _ = data
				x = Variable(x_cpu.cuda())
				batch_size = x.size(0)

				f_enc_X, f_dec_X = netD(x)

				noise = torch.cuda.FloatTensor(batch_size, args.nz, 1, 1).normal_(0, 1)
				noise = Variable(noise)
				y = netG(noise)

				f_enc_Y, f_dec_Y = netD(y)

				# compute biased MMD2 and use ReLU to prevent negative value
				mmd2_G = mix_rbf_mmd2(f_enc_X, f_enc_Y, sigma_list)
				mmd2_G = F.relu(mmd2_G)

				# compute rank hinge loss
				one_side_errG = one_sided(f_enc_X.mean(0) - f_enc_Y.mean(0))

				errG = torch.sqrt(mmd2_G) + lambda_rg * one_side_errG
				from IPython import embed; embed()
				errG.backward()
				optimizerG.step()

				gen_iterations += 1

			run_time = (timeit.default_timer() - time) / 60.0
			print('[{}/{}] D-loss {}  G-loss:{}'.format(t, args.max_iter, errD.data[0], errG.data[0]))

			if gen_iterations % 500 == 0:
				y_fixed = netG(fixed_noise)
				y_fixed = y_fixed.view(y_fixed.size(0), args.nc, args.image_size, args.image_size)
				y_fixed.data = y_fixed.data.mul(0.5).add(0.5)

				f_dec_X_D = f_dec_X_D.view(f_dec_X_D.size(0), args.nc, args.image_size, args.image_size)
				f_dec_X_D.data = f_dec_X_D.data.mul(0.5).add(0.5)

				vutils.save_image(y_fixed.data, '{0}/fake_samples_{1}.png'.format(args.experiment, gen_iterations))
				vutils.save_image(f_dec_X_D.data, '{0}/decode_samples_{1}.png'.format(args.experiment, gen_iterations))

		if t % 50 == 0:
			torch.save(netG.state_dict(), '{0}/netG_iter_{1}.pth'.format(args.experiment, t))
			torch.save(netD.state_dict(), '{0}/netD_iter_{1}.pth'.format(args.experiment, t))
