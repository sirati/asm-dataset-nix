ah i see the patch we had was for bootstrapping an old gcc from a new one yeah?

so the bootstrapping is: compile gcc11 with gcc12, now compile gcc11 with result of previous, so get fixed point. 

i see you applied the patch to older versions of gcc. but how about instead just compiling gcc10 with gcc11, now you dont need to patch anymore and then you can do gcc10 compiling gcc10 , and step by step go down the chain.
