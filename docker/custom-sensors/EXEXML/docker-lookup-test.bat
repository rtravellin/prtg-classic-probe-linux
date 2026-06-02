@echo off
REM EXEXML sensor that emits a channel with a custom <ValueLookup> reference.
REM The lookup .ovl itself is resolved by the CORE, never the probe.
echo ^<prtg^>
echo ^<result^>
echo ^<channel^>Status^</channel^>
echo ^<value^>1^</value^>
echo ^<ValueLookup^>prtg.customlookups.docker.teststatus^</ValueLookup^>
echo ^</result^>
echo ^<text^>lookup-reference test^</text^>
echo ^</prtg^>
