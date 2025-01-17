#import
import argparse
from glob import glob
import os
from PIL import Image
import astra
import numpy as np
import tensorflow as tf
from tensorflow import keras
from tensorflow.keras.models import load_model
import pdb
from Combined_Loss import combined_loss
from tensorflow.keras.layers import Layer
from tensorflow.keras import layers, models
from PIL import Image
from tensorflow.keras.preprocessing.image import load_img, img_to_array

# This function creates projectors, preprocessing our data before
# passing it to astra for the heavy lifting
def create_projector(geom, numbin, angles, dso, dod, fan_angle):
    if geom == 'parallel':
        proj_geom = astra.create_proj_geom(geom, 1.0, numbin, angles)
    elif geom == 'fanflat':
        #convert to mm for astra
        dso *=10; dod *=10;
        #compute tan of 1/2 the fan angle
        ft = np.tan( np.deg2rad(fan_angle / 2) )
        #width of one detector pixel, calculated based on fan angle
        det_width = 2 * (dso + dod) * ft / numbin

        proj_geom = astra.create_proj_geom\
                    (geom, det_width, numbin, angles, dso, dod)

    p = astra.create_projector('cuda',proj_geom,vol_geom);
    return p

# This function builds and initializes the argument parser,
# then returns the parsed arguments as they were provided on
# the command line
def generateParsedArgs():
    #Initialize parser
    parser = argparse.ArgumentParser(description='')

    parser.add_argument('--sino', dest='infile', default='.', \
        help='input sinogram -- directory or single file')

    parser.add_argument('--out', dest='outfolder', default='.', \
        help='output directory')

    parser.add_argument('--numpix', dest='numpix', type=int, default=512, \
        help='size of volume (n x n )')

    parser.add_argument('--psize', dest='psize', default='', \
        help='pixel size (float) OR file containing pixel sizes (string)');

    parser.add_argument('--numbin', dest='numbin', type=int, default=729, \
        help='number of detector pixels')

    parser.add_argument('--ntheta', dest='numtheta', type=int, default=900, \
        help='number of angles')

    parser.add_argument('--nsubs', dest='ns', type=int, default=1, \
        help='number of subsets. must divide evenly into number of angles')

    parser.add_argument('--range', dest='theta_range', type=float, nargs=2, \
                        default=[0, 360], \
        help='starting and ending angles (deg)')

    parser.add_argument('--geom', dest='geom', default='fanflat', \
        help='geometry (parallel or fanflat)')

    parser.add_argument('--dso', dest='dso', type=float, default=100, \
        help='source-object distance (cm) (fanbeam only)')

    parser.add_argument('--dod', dest='dod', type=float, default=100, \
        help='detector-object distance (cm) (fanbeam only)')

    parser.add_argument('--fan_angle', dest='fan_angle', default=35, type=float, \
        help='fan angle (deg) (fanbeam only)')

    parser.add_argument('--numits', dest='num_its', default=32, type=int, \
        help='maximum number of iterations')

    parser.add_argument('--beta', dest='beta', default=1., type=float, \
        help='relaxation parameter beta')

    parser.add_argument('--x0', dest='x0_file',default='', \
        help='initial image (default: zeros)')

    parser.add_argument('--xtrue', dest='xtrue_file', default='', \
        help='true image (if available)')

    parser.add_argument('--sup_params', dest='sup_params', type=float, nargs=4,\
        help='superiorization parameters: k_min, k_step, gamma, bm3d_sigma')

    parser.add_argument('--epsilon_target', dest='epsilon_target', default=0., \
        help='target residual value (float, or file with residual values)')

    parser.add_argument('--ckpt_dir', dest='ckpt_dir', default='./checkpoint', \
        help='directory containing checkpoint for DnCnn')

    parser.add_argument('--make_png', dest='make_png',type=bool, default=False,\
        help='whether or not you would like to generate .png files')

    parser.add_argument('--make_intermediate', dest='make_intermediate', \
                        type=bool, default=False,\
        help='whether or not you would like to generate output files each iter')

    parser.add_argument('--overwrite', dest='overwrite', \
                        type=bool, default=True,\
        help='whether you would like to reprocess preexisting files on export')

    #Return arguments as parsed from command line
    return parser.parse_args()


# This function outputs a .png
def makePNG(f, outname):
    #Set any negative values to positive machine epsilon
    img = np.maximum(f,np.finfo(float).eps)
    #Scale to [0,255]
    img = (img.T/np.amax(f)) * 255
    #Discretize
    img = np.round(img)
    #Convert to int
    img = Image.fromarray(img.astype('uint8')).convert('L')
    #Save it
    img.save(outname + '.png','png')
# This function outputs a .flt
def makeFLT(f, outname):
    #Convert to float32
    img = np.float32(f)
    #Set any negative values to positive machine epsilon
    img = np.maximum(img,np.finfo(np.float32).eps)
    #Save it
    img.tofile(outname + '.flt')
    return

def sart_loop(ns, numtheta, f, p):
    def numpy_sart_loop(f):
        for j in range(ns):
            ind1 = range(j, numtheta, ns)
            p = P[j]
            if isinstance(f, tf.Tensor):
                f = f.numpy()
                f = f.astype(np.float64)
            # Forward projection step
            fp_id, fp = astra.create_sino(f, p)
            # Perform elementwise division
            diffs = (sino[ind1, :] - fp * dx) / Minv[j] / dx
            bp_id, bp = astra.create_backprojection(diffs, p)
            #Get rid of spurious large values
            ind2 = np.abs(bp) > 1e3
            bp[ind2] = 0
            #Update f
            f = f + beta * bp / Dinv[j]
            astra.data2d.delete(fp_id)
            astra.data2d.delete(bp_id)
        f = np.maximum(f, eps)
        return f

    f = tf.py_function(func=numpy_sart_loop,inp=[f], Tout=tf.float32)
    return f

class IterativeLayer(layers.Layer):
    def __init__(self, layer, threshold=0.01, max_iterations=10, **kwargs):
        super(IterativeLayer, self).__init__(**kwargs)
        self.layer = layer
        self.threshold = threshold
        self.max_iterations = max_iterations
    def compute_residual(f, p, numtheta, numbin, ns, sino, dx, calc_error, xtrue, k):
        fp = np.zeros((numtheta, numbin))
        for j in range(ns):
            ind = range(j, numtheta, ns)
            p = P[j]
            fp_tempid, fp_temp = astra.create_sino(f, p)
            fp[ind, :] = fp_temp * dx
            astra.data2d.delete(fp_tempid)
            res = np.linalg.norm(fp - sino, 'fro')
            # Error checking
            if calc_error:
                err = np.linalg.norm(f - xtrue, 'fro') / np.linalg.norm(xtrue, 'fro')
                print('Iteration #{0:d}: Residual = {1:1.4f}\tError = {2:1.4f}\n'.format(k, res, err))
            else:
                print('Iteration #{0:d}: Residual = {1:1.4f}\n'.format(k, res))
        return res

    def call(self, inputs, training=None):
        output = inputs
        iteration = 0

        while iteration < self.max_iterations:
            output = self.layer(output)
            combined = tf.concat([sart_loop(ns,numtheta,output,p), output], axis=0)
            residual = self.compute_residual(combined, p, numtheta, numbin,ns, sino, dx, calc_error, xtrue, k)
            if residual > self.threshold:
                output = combined
            iteration += 1
            print(f'iterate: {iteration}')
        return output

# Main                                                                         #

# Parse Arguments & Initialize #
#Get parsed arguments
args = generateParsedArgs()

#Split them up
infile =  args.infile       #input sinogram -- directory or single file
outfolder = args.outfolder  #output directory
x0file = args.x0_file       #initial image (default: zeros)
xtruefile = args.xtrue_file #true image (if available)
psize = args.psize          #pixel size (float) OR file containing pixel sizes
numpix = args.numpix        #size of volume (n x n )
numbin = args.numbin        #number of detector pixels
numtheta = args.numtheta    #number of angles
ns = args.ns                #number of subsets. must divide numtheta evenly
numits = args.num_its       #maximum number of iterations
beta = args.beta            #relaxation parameter beta
epsilon_target = args.epsilon_target #target residual value to stop
theta_range = args.theta_range       #starting and ending angles (deg)
geom = args.geom            #geometry (parallel or fanflat)
dso = args.dso              #source-object distance (cm) (fanbeam only)
dod = args.dod              #detector-object distance (cm) (fanbeam only)
fan_angle = args.fan_angle  #fan angle (deg) (fanbeam only)
make_png = bool(args.make_png)    #whenther or not we will be exporting .png
overwrite = bool(args.overwrite)  #whether we reprocess preexisting files
make_intermediate = bool(args.make_intermediate)  #whether or not you would
                                            #like to generate output each iter

#Were superiorization parameters provided?
use_sup = False
kmin = 0    #Iteration at which superiorization begins
kstep = 0   #Interval of SARTS between each superiorization step
gamma = 0   #Geometric attenuation factor for superiorization
sigma = 0   #The parameter for BM3D
alpha = 1   #Computed attenuation factor for superiorization, not an arg
if not (args.sup_params is None):
    use_sup = True
    kmin = int(args.sup_params[0])
    kstep = int(args.sup_params[1])
    gamma = args.sup_params[2]
    sigma = args.sup_params[3]

#Get machine epsilon for the float type we are using
eps = np.finfo(float).eps

#Generate list of filenames from directory provided
fnames = []
if os.path.isdir(infile):
    fnames = sorted(glob(infile + '/*.flt'))
#Otherwise, a single filename was provided
else:
    fnames.append(infile)

#If pixel size was provided as a floating point value
psizes = 0
try:
    psizes = float(psize)
#Otherwise, a filename was given
except ValueError:
    psizes = np.loadtxt(psize,dtype='f')

#If target residual was provided as a single value
try:
    epsilon_target = float(epsilon_target)
#Otherwise, a file was provided
except ValueError:
    epsilon_target = np.loadtxt(epsilon_target,dtype='f')

#Create projection geometry
vol_geom = astra.create_vol_geom(numpix, numpix)

#Generate array of angular positions
theta_range = np.deg2rad(theta_range) #convert to radians
angles = theta_range[0] + np.linspace(0,numtheta-1,numtheta,False) \
         *(theta_range[1]-theta_range[0])/numtheta

calc_error = False

#Create projectors and normalization terms, corresponding to
#diagonal matrices M and D, for each subset of projection data
P, Dinv, D_id, Minv, M_id = [None]*ns,[None]*ns,[None]*ns,[None]*ns,[None]*ns
for j in range(ns):
    ind1 = range(j,numtheta,ns);
    p = create_projector(geom,numbin,angles[ind1],dso,dod,fan_angle)

    D_id[j], Dinv[j] = \
             astra.create_backprojection(np.ones((numtheta//ns,numbin)),p)
    M_id[j], Minv[j] = \
             astra.create_sino(np.ones((numpix,numpix)),p)
    #Avoid division by zero, also scale M to pixel size
    Dinv[j] = np.maximum(Dinv[j],eps)
    Minv[j] = np.maximum(Minv[j],eps)
    P[j] = p

#Open the file for storing residuals
res_file = open(outfolder + "/residuals.txt", "w+")
res = 0

#For each filename provided
for n in range(len(fnames)):
#Per-image initialization:
#Get filename for output
    name = fnames[n]
    head, tail = os.path.split(name)
    #Extract numerical part of filename only. Assumes we have ######_sino.flt
    head, tail = tail.split("_",1)
    outname = outfolder + "/" + head + "_recon_"
    print("\nReconstructing " + head + ":")

    #Read in sinogram
    sino = np.fromfile(name,dtype='f')
    sino = sino.reshape(numtheta,numbin)

    #Create a new square nparray for the image size we have
    f = np.zeros((numpix,numpix))

    #Get new psize if they're being read from a file
    try:
        dx = psizes[n]
    #Otherwise, psize is a float
    except:
        dx = psizes

    #Same for the target residuals
    try:
        etarget = epsilon_target[n]
    except:
        etarget = epsilon_target

    # Single-image Finalization
    #Write the final residual to the file for this image
    res_file.write("%f\n" % res)
    if use_sup:
        makeFLT(f, outname + str(k) + '_BM3Dsup')
    if make_png:
        makePNG(f, outname + str(k) + '_BM3Dsup')
    else:
        makeFLT(f, outname + str(k) + '_SART')
    if make_png:
        makePNG(f, outname + str(k) + '_SART')

# CNN                     #
def iterative_model(input_size=(512, 512, 1)):
      inputs = tf.keras.Input(input_size)
          # Downsample
      x = IterativeLayer(layers.Conv2D(32, (3, 3), activation='relu'))(inputs)
      x = layers.MaxPooling2D((2, 2))(x)
      x = IterativeLayer(layers.Conv2D(64, (3, 3), activation='relu'))(x)
      x = layers.MaxPooling2D((2, 2))(x)
      x = IterativeLayer(layers.Conv2D(64, (3, 3), activation='relu'))(x)

      outputs = layers.Conv2D(3, (1, 1), activation='sigmoid')(c9)
      model = models.Model(inputs=inputs, outputs=[outputs])

      return model

def load_images_from_directory(directory, target_size=(512, 512)): #Defines a data loading function with parameters "directory" and size of image
    images = []                   #Creates an empty array called images
    for filename in os.listdir(directory): #For each file in the directory argument do
        if filename.endswith(".png"): #If it is a png file (This can be modified to flt)
            img = load_img(os.path.join(directory, filename), target_size=target_size) #Loads the images from the directory given
            img = img_to_array(img)       #defines the img variable as the argument of an image to array function
            img = img / 255.0  #Normalizes pixel values
            images.append(img)  #adds each file in the directory to the end of the array in order
    return np.array(images) #returns the array of images



# CNN Directories         #
# Directories
clean_dir = infile
dirty_dir = '/mmfs1/gscratch/uwb/bkphill2/60_views'

clean_images = load_images_from_directory(clean_dir)
dirty_images = load_images_from_directory(dirty_dir)

# Split dataset
X_train, y_train = dirty_images, clean_images
# Create U-Net model
model = iterative_model()
model.compile(optimizer='adam', loss=combined_loss, metrics=['accuracy'])

# Train model
model.fit(X_train, y_train, validation_split=0.1, epochs=5, batch_size=16)

# Save model
model.save('iterative_model.h5')

print("\n\nExiting...")

#Cleanup
for j in range(ns):
    astra.data2d.delete(D_id[j])
    astra.data2d.delete(M_id[j])
    astra.projector.delete(P[j])
res_file.close()

