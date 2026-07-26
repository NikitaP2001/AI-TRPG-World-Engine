General requirements.

1. Agent context
1.0 General rules
  Formal and abstraction wording over clarity - we built formal framework, 
  suitable for any scenario which could be expressed in natural language.
  
1.1 Inside SINGLE turn EACH (GM, SM, WM, CH) agent have SINGLE invokation conext.
 General explanation In other words, if agent inside ONE turn was stoped, but then
 reinvoked again the the same turn - agent should not notice he was reinvoked.
 More specific - agent see all his context, which was left when he was stopped.
 Even more detailed - we store persistent egent entity memory in correstponding agent
 messages.json. Based on this file the agent context is built between invokation.
 But this messages json is optimal in size - id does not store everything. But many
 valuable info, which is not worth sotirng in long (in messages.json) is present inside
 invokation but later NOT saved in messages - such as agent thoughts, invokation-scale pinned information, 
 notification, called tools, etc. Everyting this SHOULD persist inside single turn, not
 matter how manny times agent was reinvoked - or he will become lost.

1.2. Tool comments AND ANY PROMPT INJECTIONS are full, formal and abstract but OPTIMAL in size:
 - Comments do not include examples, internal explanation (how the system work inside)
 (explanation: with tools comments and pormpt we draw some picture to the agent,
 how this `game`(or conceptual framework) looks from his side. Comments must not include anything, which breaks
 agent context, view point or immersion (refer something out of agent conceptual framework)
 - Tool Comments should not contain excessive markdown or spacing - they will waste prompt.
 - this touch not only tools but also any guides, rules, requests etc, which is included in agent context.
 
1.3 Agent context does not contain duplication
 None information is placed in context if it already present here. This also includes
 general-instructions, not only story-related messages.
 This is hard task - it`s not easlity traced in code. We have multiple places 
 of context injections, we have:
 Top level caches - with most persistant info
 We have messages json with less, but persistant info which is frequently trimmed,
 but presist between different turns.
 In the end we have single turn context (invokation inside single turn),
 which includes everything agent was told or produced.
 So all 3 is agent context. And inside this whole context no info or instruction should
 be provided twice.
 
1.4 Agent messages storage is updated only ONCE per turns
 While we persist agent invocation context inside single turn BUT
 messages storage (more persistant) is updated by fixed valuable set of info
 only on the end of turn.
 
1.5 Agent is NOT told what he MUST do (or what tool to use) now or what is set of avaliable actions to him
 The agent behaviour is IMPLICITELY determined by our conceptual framework.
 We do not generally tell agent to which actions he should stick now - this 
 mask real issues in our framework and waste prompt.

1.6 No empty context entires
  Agents never receive empty placeholders for something which is not empty, and which waste context space.
  Everyting what is injected MUST contain new meaninful information inside.
  Generally watch for any duplication in context logs, or empty entries, or bloated entires, etc

2. Tools
2.1 Immediate error reaction.
All tools have specific set of expected values and validation.
On any error we do not try to save it as is, recover, or ignore - we immediately
re pormpt agent with clear error message, saying what specifically was wrong.

2.2. Single successful tool should be called only once.
    This is usually not visible in pure code, but we must check that agent 
    did not duplicate tool calls in real runtime, othervise there is 
    some context-building error which confuses the agent and we have to identify it.
    
3. Scene
(this usually visible only on real game logs, not code)

3.0 Characters fully control their own, nothing happens with their will without their intent.
 In first order this IS NOT story, this is GAME. Without character CLEAR intent, he is not doing something, even idle or obvious.

3.1 After turn each character agent have information about everything he may realistially percieve AND ONLY IT.

3.2 Each character All perception and thinking attempts are adressed by SM and delivered to character before turn end.

3.3. There are no Synchronization or timing confuses.
  Explanation - there are no cases where character EXPRESS actions or thoughts which they wont express
  in such case in reality but produce only because they are confused because of our implementation.
  There are not dialogue confuses, where one character think other is ignoring him, while at the same time other pary planed response
  
  
3.4. No info is conflabuated without storage support.
      - Every elemental information piece, evidencec, relation or fact should be implied from storage OR stored directly after it was produced.
      (In other words we accept SM and other agents to fix-up things back in time, if something was stored eventually)
           
3.5 At the end of the turn, or at right the begining of other all characters intents should be resolved.  
  for example but not limiting character speech, or actions are correctly outlined, location switches processed (to other location or inside one location).
  
3.6 Everyting in SM and Char agent contexts is time-aware
  - Verify Agents are fully avare about current time, each planned event time, and each occured event time

3.7 Character which planned actions longer then scene interval are not included in scene
  - the mechanism where some character which stick to idle actions AND are not interested are
  left doing their actions
  
3.8 Ensure progress after reinvoke
  - The character which on previous turns was doing something out of scene (idle) is included - 
  his progress is updated (locations, inventory, stats, etc..) by scene manager in some way and on his first invokation
  he receives detailed explanations what happened while he was idle.
    
4. Storage
4.1 Complete time avarness - each, EACH storage saved piece of information have world time stamp.
 AND this implies on injection this time stamp is PRESENT. This works together tightly with 3.6 requirement
 
   
5. Character   
5.1 Character never experience `teleportation` and `time travel` without explicit explanation
  - Characters in the next scene did not receive description and have location, so based on their last 
  scene it appeared like they teleported without any intent, and they are not explained what happened.
 
5.2 Character have pocivility to update his perception and awarness INSIDE turn.
 Character may look for something, thinkg, recall to knowledge etc INSIDE turn, and his recalls are adressed properly
 
 
5.3. Full context isolation - Character AGENT have NO meta info in his context
 Inside agent context there are no pieces of information which his character realistically could not know or percieve

  
6 Scene Manager

1.1 Scene Manager did not invent anything which could imply global-scale details not already present ins torage
 Scene Manger could create only local-scale events inside current sceene. For everything which could imply or have deduced more global details - he should escalate or search the storage.