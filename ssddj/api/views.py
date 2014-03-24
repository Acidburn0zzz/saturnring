from ssdfrontend.models import Target
from ssdfrontend.models import StorageHost
from ssdfrontend.models import LV
from ssdfrontend.models import VG
from globalstatemanager.gsm import PollServer
from utils.affinity import RandomAffinity
from serializers import TargetSerializer
from serializers import ProvisionerSerializer
from serializers import VGSerializer
from django.db.models import Q
from django.db.models import F
from rest_framework.authentication import SessionAuthentication, BasicAuthentication
from rest_framework.permissions import IsAuthenticated
from django.core.exceptions import ObjectDoesNotExist
from django.http import Http404
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.views.decorators.csrf import csrf_exempt
import random
import logging
logger = logging.getLogger(__name__)
from utils.scstconf import ParseSCSTConf
class Provisioner(APIView):
    authentication_classes = (SessionAuthentication, BasicAuthentication)
    permission_classes = (IsAuthenticated,)
    def get(self, request ):
        serializer = ProvisionerSerializer(data=request.DATA)
        if serializer.is_valid():
            iqntar = self.MakeTarget(request.DATA,request.user)
#            serializer.save()
            if iqntar <> 0:
                tar = Target.objects.filter(iqntar=iqntar)
                data = tar.values()
                #data = serializers.serialize('json', list(Target.objects.get(iqntar=iqntar)), fields=('iqnini','iqntar'))a
                return Response(data, status=status.HTTP_201_CREATED)
            else:
                return Response(serializer.errors, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        else:
            logger.warn("Invalid provisioner serializer data: "+str(request.DATA))
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def UpdateLVs(self,vg):
        p = PollServer(vg.vghost)
        lvdict = p.GetLVs("storevg")
        lvs = LV.objects.all()
        for eachLV in lvs:
            eachLV.lvsize=lvdict[eachLV.lvname]['LV Size']
            eachLV.lvthinmapped=lvdict[eachLV.lvname]['Mapped size']
            eachLV.save(update_fields=['lvsize','lvthinmapped'])

    def VGFilter(self,storageSize):
        # Check if StorageHost is enabled
        # Check if VG is enabled
        # Find all VGs where SUM(Alloc_LVs) + storageSize < opf*thintotalGB
        # Further filter on whether thinusedpercent < thinusedmaxpercent
        # Return a random choice from these
        storagehosts = StorageHost.objects.filter(enabled=True)
        logger.info("Found %d storagehosts" %(len(storagehosts),))
        for eachhost in storagehosts:
            p = PollServer(eachhost.ipaddress)
            p.GetVG()
        vgchoices = VG.objects.filter(enabled=True,thinusedpercent__lt=F('thinusedmaxpercent'))

        if len(vgchoices) > 0:
            numDel=0
            for eachvg in vgchoices:
                p = PollServer(eachvg.vghost) # Check this
                self.UpdateLVs(eachvg)
                lvs = LV.objects.filter(vg=eachvg)
                lvalloc=0.0
                for eachlv in lvs:
                   lvalloc=lvalloc+eachlv.lvsize
                if (lvalloc + float(storageSize)) > (eachvg.thintotalGB*eachvg.opf):
                   logger.info("Disqualified %s/%s, because %f > %f" %(eachvg.vghost,eachvg.vguuid,lvalloc+float(storageSize),eachvg.thintotalGB*eachvg.opf))
                   numDel=numDel+1 
                else:
                    logger.info("A qualified choice for Host/VG is %s/%s" %(eachvg.vghost,eachvg.vguuid))
            if len(vgchoices)>numDel:
                chosenVG = random.choice(vgchoices)
                logger.info("Chosen Host/VG combo is %s/%s" %(chosenVG.vghost,chosenVG.vguuid))
                return chosenVG
            else:
                logger.info("No VG that satisfies the overprovisioning contraint (opf) was found")
                return -1
        else:
            logger.warn('No vghost/VG enabled')
            return -1

    def MakeTarget(self,requestDic,owner):
        clientStr = requestDic['clienthost']
        serviceName = requestDic['serviceName']
        storageSize = requestDic['sizeinGB']
        #first query each host for vg capacity(?)
        #do this in parallel using a thread per server
        logger.info("Provisioner - request received: %s %s %s" %(clientStr, serviceName, str(storageSize)))
        #vgchoices = VG.objects.filter(maxthinavlGB__gt=float(storageSize))
        #logger.info("VG choices are %s " %(str(vgchoices),))
        #chosenVG = random.choice(vgchoices)
        chosenVG = self.VGFilter(storageSize)
        if chosenVG <> -1:
            targetHost=str(chosenVG.vghost)
            iqnTarget = "".join(["iqn.2014.01.",targetHost,":",serviceName,":",clientStr])
            try:
                t = Target.objects.get(iqntar=iqnTarget)
                logger.info('Target already exists: %s' % (iqnTarget,))
                return iqnTarget
            except ObjectDoesNotExist:
                logger.info("Creating new target for request {%s %s %s}, this is the generated iSCSItarget: %s" % (clientStr, serviceName, str(storageSize), iqnTarget))
                targetIP = StorageHost.objects.get(dnsname=targetHost)
                p = PollServer(targetIP.ipaddress)
                if (p.CreateTarget(iqnTarget,str(storageSize))):
                    p.GetTargets()
                    (devDic,tarDic)=ParseSCSTConf('config/'+targetIP.ipaddress+'.scst.conf')
                    if iqnTarget in tarDic:
                        newTarget = Target(owner=owner,targethost=chosenVG.vghost,iqnini=iqnTarget+":ini",
                            iqntar=iqnTarget,clienthost=clientStr,sizeinGB=float(storageSize))
                        newTarget.save()
                        lvDict=p.GetLVs()
                        if devDic[tarDic[iqnTarget][0]] in lvDict:
                            newLV = LV(target=newTarget,vg=chosenVG,
                                    lvname=devDic[tarDic[iqnTarget][0]],
                                    lvsize=storageSize,
                                    lvthinmapped=lvDict[devDic[tarDic[iqnTarget][0]]]['Mapped size'],
                                    lvuuid=lvDict[devDic[tarDic[iqnTarget][0]]]['LV UUID'])
                            newLV.save()
                    
                    return iqnTarget
                else:
                    logger.warn('CreateTarget did not work')
                    return 0
        else:
            logger.warn('VG filtering did not return a choice')
            return  "No suitable Saturn server was found to accomadate your request"

class VGScanner(APIView):
    authentication_classes = (SessionAuthentication, BasicAuthentication)
    permission_classes = (IsAuthenticated,)
    def get(self, request):
        logger.info("VG scan request received: %s " %(request.DATA,))
        saturnserver=request.DATA[u'saturnserver']
        if (StorageHost.objects.filter(Q(dnsname__contains=saturnserver) | Q(ipaddress__contains=saturnserver))):
            p = PollServer(saturnserver)
            savedvguuid = p.GetVG()
            readVG=VG.objects.filter(vguuid=savedvguuid).values()
            return Response(readVG)
        else:
            logger.warn("Unknown saturn server "+str(request.DATA))
            return Response("Unregistered or uknown Saturn server "+str(request.DATA), status=status.HTTP_400_BAD_REQUEST)

class TargetDetail(APIView):
    """
    Retrieve, update or delete a target instance.
    """
    def get_object(self, pk):
        try:
            return Target.objects.get(pk=pk)
        except Target.DoesNotExist:
            raise Http404

    def get(self, request, pk, format=None):
        target = self.get_object(pk)
        serializer = TargetSerializer(target)
        return Response(serializer.data)

    def put(self, request, pk, format=None):
        target = self.get_object(pk)
        serializer = TargetSerializer(target, data=request.DATA)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def delete(self, request, pk, format=None):
        target = self.get_object(pk)
        target.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

