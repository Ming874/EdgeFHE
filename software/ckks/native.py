"""Local compiled shared backend. Board hardware calls require external gates."""
from array import array
import ctypes
from pathlib import Path
import time
from profile import N,Q,require,validate


class PhaseTimes(ctypes.Structure):
    _fields_=[(name,ctypes.c_uint64) for name in ['allocation_ns','pack_ns','setup_ns','ioctl_ns','unpack_check_ns','total_ns']]+[('driver_us',ctypes.c_uint32),('reserved',ctypes.c_uint32)]


def flatten(poly):
    validate(poly);words=array('I',(x for limb in poly for x in limb))
    require(words.itemsize==4,'Native uint32 required')
    return (ctypes.c_uint32*(3*N)).from_buffer(words),words


class Backend:
    def __init__(self,path,fd=None):
        require(ctypes.sizeof(PhaseTimes)==56 and PhaseTimes.driver_us.offset==48,'Timing ABI mismatch')
        self.library=ctypes.CDLL(str(Path(path)),use_errno=True);self.fd=fd;self.records=[]
        pointer=ctypes.POINTER(ctypes.c_uint32)
        self.library.edgefhe_cpu.argtypes=[ctypes.c_uint32,pointer,pointer,pointer];self.library.edgefhe_cpu.restype=ctypes.c_int
        self.library.edgefhe_hardware_multiply.argtypes=[ctypes.c_int,pointer,pointer,pointer,ctypes.POINTER(PhaseTimes)];self.library.edgefhe_hardware_multiply.restype=ctypes.c_int

    def cpu(self,a,b=None,op=2):
        av,keep_a=flatten(a);bv,keep_b=flatten(b) if b is not None else (None,None)
        out=(ctypes.c_uint32*(3*N))()
        ret=self.library.edgefhe_cpu(op,av,bv,out);require(ret==0,'CPU reference failure status='+str(ret))
        result=[list(out[l*N:(l+1)*N]) for l in range(3)];validate(result);return result

    def multiply(self,a,b,label):
        expected=self.cpu(a,b)
        if self.fd is None:return expected
        av,keep_a=flatten(a);bv,keep_b=flatten(b);out=(ctypes.c_uint32*(3*N))();times=PhaseTimes()
        ret=self.library.edgefhe_hardware_multiply(self.fd,av,bv,out,ctypes.byref(times))
        require(ret==0,'Native hardware failure '+label+' status='+str(ret)+'; stop without retry')
        result=[list(out[l*N:(l+1)*N]) for l in range(3)];validate(result)
        require(result==expected,'Hardware/CPU coefficient mismatch '+label)
        fields={name:int(getattr(times,name)) for name,_ in PhaseTimes._fields_}
        require(fields['reserved']==0 and sum(fields[k] for k in ['allocation_ns','pack_ns','setup_ns','ioctl_ns','unpack_check_ns'])==fields['total_ns'],'Phase timing sum mismatch')
        require(fields['driver_us']>0 and fields['driver_us']*1000<=fields['ioctl_ns']+1000,'Driver interval outside ioctl')
        self.records.append({'label':label,'operation':2,'coefficient_comparisons':3*N,'padding_words_checked':N,'status':'PASS',**fields})
        return result
