-- LoganReads data file.
-- Keyed by quest ID (numbers) AND by the original quest title (strings) as fallback.
-- To add a quest: add one row in this file. Fields:
--   title  = new kid title
--   todo   = what to do line
--   story  = the story text (replaces the quest description)
--   notDone= progress-window text (empty string = not used)
--   done   = reward-window text
--   origTitle must match the server's LogTitle exactly.

local D = {}

D[783] = {
	id = 783,
	origTitle = "A Threat Within",
	title = "Go See McBride!",
	todo = "Go see McBride.",
	story = "Hi! I am Willem. Go see McBride. He is in the big house. He needs help. Go!",
	notDone = "",
	done = "Hi! I am McBride. I am glad you are here.",
}
D["A Threat Within"] = D[783]

D[5261] = {
	id = 5261,
	origTitle = "Eagan Peltskinner",
	title = "Go See Eagan!",
	todo = "Go see Eagan.",
	story = "Hi! I am Willem. Go see Eagan. He is out back. He has a job for you.",
	notDone = "",
	done = "Hi! I am Eagan. I have a job for you.",
}
D["Eagan Peltskinner"] = D[5261]

D[7] = {
	id = 7,
	origTitle = "Kobold Camp Cleanup",
	title = "Beat the Kobolds!",
	todo = "Beat 10 Kobold Vermin. Go back to McBride.",
	story = "Bad kobolds are here. Can you help? Beat 10 Kobold Vermin. Then come back to me.",
	notDone = "Did you beat 10 Kobold Vermin?",
	done = "You did it! Good job!",
}
D["Kobold Camp Cleanup"] = D[7]

D[33] = {
	id = 33,
	origTitle = "Wolves Across the Border",
	title = "Get Wolf Meat!",
	todo = "Get 8 Tough Wolf Meat. Go back to Eagan.",
	story = "I need wolf meat. Beat the wolves. Get 8 Tough Wolf Meat. Then come back to me.",
	notDone = "Do you have 8 Tough Wolf Meat?",
	done = "Yum! Thank you!",
}
D["Wolves Across the Border"] = D[33]

D[15] = {
	id = 15,
	origTitle = "Investigate Echo Ridge",
	title = "Kobolds by the Mine!",
	todo = "Beat 10 Kobold Workers. Go back to McBride.",
	story = "More bad kobolds! They are by the mine. Beat 10 Kobold Workers. Then come back to me.",
	notDone = "Did you beat 10 Kobold Workers?",
	done = "You did it! Good job!",
}
D["Investigate Echo Ridge"] = D[15]

LoganReads_Data = D

-- ===== Batch 1: Northshire remainder + all Goldshire starters =====

D[3100] = {
	id = 3100,
	origTitle = "Simple Letter",
	title = "A Letter for You!",
	todo = "Talk to Llane. He is in the big house.",
	story = "I have a letter. It is for you. Llane helps warriors. Talk to Llane.",
	notDone = "Did you talk to Llane?",
	done = "Good! I can help you. Come back any time.",
}
D["Simple Letter"] = D[3100]

D[18] = {
	id = 18,
	origTitle = "Brotherhood of Thieves",
	title = "Get the Bandanas!",
	todo = "Get 12 Red Burlap Bandana. Go back to Willem.",
	story = "Bad men are here. They are not far. They have red bandanas. Bring me 12 of them. I have a prize!",
	notDone = "Do you have 12 Red Burlap Bandana?",
	done = "Thank you! Take this prize. Good job!",
}
D["Brotherhood of Thieves"] = D[18]

D[6] = {
	id = 6,
	origTitle = "Bounty on Garrick Padfoot",
	title = "Get Garrick!",
	todo = "Beat Garrick. Get 1 Garrick's Head. Go back to Willem.",
	story = "Garrick is a bad man. His men are with him. Find his house. It is by the bridge. Be careful!",
	notDone = "Did you find Garrick?",
	done = "You got him! Take your prize. Good job!",
}
D["Bounty on Garrick Padfoot"] = D[6]

D[3903] = {
	id = 3903,
	origTitle = "Milly Osworth",
	title = "Go See Milly!",
	todo = "Go see Milly. She is by the stable.",
	story = "My friend Milly needs help. She is by the stable. Her wagon is there. Go see her.",
	notDone = "",
	done = "Thank you for coming! I need your help.",
}
D["Milly Osworth"] = D[3903]

D[3904] = {
	id = 3904,
	origTitle = "Milly's Harvest",
	title = "Save the Grapes!",
	todo = "Get 8 Milly's Harvest. Go back to Milly.",
	story = "Hi! I am Milly. Bad men have my grapes. My buckets are out there. Bring them to me. Be fast!",
	notDone = "Do you have 8 Milly's Harvest?",
	done = "You saved my grapes! Thank you!",
}
D["Milly's Harvest"] = D[3904]

D[3905] = {
	id = 3905,
	origTitle = "Grape Manifest",
	title = "Take the Paper!",
	todo = "Take the Grape Manifest to Neals. He is in the big house.",
	story = "My grapes are safe. Take this paper to Neals. He is in the big house. He will be happy.",
	notDone = "Did you give Neals the paper?",
	done = "The grapes are safe! Good job! Thank you!",
}
D["Grape Manifest"] = D[3905]

D[21] = {
	id = 21,
	origTitle = "Skirmish at Echo Ridge",
	title = "More Kobolds!",
	todo = "Beat 12 Kobold Laborer. Go back to McBride.",
	story = "More kobolds are in the mine. We need to beat them. Beat 12 Kobold Laborer. Then come back to me.",
	notDone = "Did you beat 12 Kobold Laborer?",
	done = "You did it! Good job! You keep us safe.",
}
D["Skirmish at Echo Ridge"] = D[21]

D[54] = {
	id = 54,
	origTitle = "Report to Goldshire",
	title = "Go See Dughan!",
	todo = "Take the Marshal McBride's Documents to Dughan. He is in Goldshire.",
	story = "You did big work. Take this paper to Dughan. He is in Goldshire. Go down the road.",
	notDone = "Did you talk to Dughan?",
	done = "Good papers! You are a helper now. Good job!",
}
D["Report to Goldshire"] = D[54]

D[47] = {
	id = 47,
	origTitle = "Gold Dust Exchange",
	title = "Get the Gold Dust!",
	todo = "Get 10 Gold Dust. Go back to Remy.",
	story = "Hi! I am Remy. Kobolds have Gold Dust. Look in the mines. Get 10 Gold Dust. I will pay you!",
	notDone = "Do you have 10 Gold Dust?",
	done = "Thanks for the dust! Here is your prize.",
}
D["Gold Dust Exchange"] = D[47]

D[40] = {
	id = 40,
	origTitle = "A Fishy Peril",
	title = "Talk to Dughan!",
	todo = "Talk to Dughan.",
	story = "Bad fish men are here! They are in the east. Dughan does not hear me. Go talk to him!",
	notDone = "",
	done = "Thank you. I will look for Murlocs.",
}
D["A Fishy Peril"] = D[40]

D[62] = {
	id = 62,
	origTitle = "The Fargodeep Mine",
	title = "Look in the Mine!",
	todo = "Look in the Fargodeep Mine. Go back to Dughan.",
	story = "Hi! I am Dughan. There is a south mine. Bad kobolds are in it. Go look in the mine. Then come back to me.",
	notDone = "Did you look in the mine?",
	done = "Kobolds in the mine! Thank you for looking.",
}
D["The Fargodeep Mine"] = D[62]

D[76] = {
	id = 76,
	origTitle = "The Jasperlode Mine",
	title = "The East Mine!",
	todo = "Look in the Jasperlode Mine. Go back to Dughan.",
	story = "There is another mine. It is east of here. Go look in the Jasperlode Mine. Is it full of kobolds? Come back to me.",
	notDone = "Did you look in the mine?",
	done = "Kobolds there too? Bad news! Thank you for looking.",
}
D["The Jasperlode Mine"] = D[76]

D[35] = {
	id = 35,
	origTitle = "Further Concerns",
	title = "Talk to Thomas!",
	todo = "Talk to Thomas. He is at the bridge.",
	story = "A guard is at the bridge. His name is Thomas. He knows about the east. Go talk to Thomas.",
	notDone = "",
	done = "Hello! I watch the bridge. Murlocs are here. Be careful!",
}
D["Further Concerns"] = D[35]

D[59] = {
	id = 59,
	origTitle = "Cloth and Leather Armor",
	title = "Give Sara the Marker!",
	todo = "Give the Stormwind Armor Marker to Sara. She is at the log camp.",
	story = "Here is a marker. Sara can make armor. She is at the log camp. It is far east. Give her the marker.",
	notDone = "Do you have the marker?",
	done = "Thank you for the marker! Look at my armor. Take one! It is for you.",
}
D["Cloth and Leather Armor"] = D[59]

D[60] = {
	id = 60,
	origTitle = "Kobold Candles",
	title = "Get the Candles!",
	todo = "Get 8 Large Candle. Go back to William.",
	story = "Hi! I am William. I need candles. Kobolds have them. Look in the mines. Get 8 Large Candle.",
	notDone = "Do you have 8 Large Candle?",
	done = "Thank you for the candles! My brother will be glad.",
}
D["Kobold Candles"] = D[60]

D[61] = {
	id = 61,
	origTitle = "Shipment to Stormwind",
	title = "Take the Shipment!",
	todo = "Take William's Shipment to Morgan. He is in Stormwind.",
	story = "My brother needs this box. His name is Morgan. He is in Stormwind. It is a long walk. Take him the box.",
	notDone = "Did you give Morgan the box?",
	done = "My box is here! Thank you! Here is your pay.",
}
D["Shipment to Stormwind"] = D[61]

D[112] = {
	id = 112,
	origTitle = "Collecting Kelp",
	title = "Get the Kelp!",
	todo = "Get 4 Crystal Kelp Frond. Go back to William.",
	story = "Maybell needs a drink. I can make it. I need kelp. Look at the lake. Get 4 Crystal Kelp Frond.",
	notDone = "Do you have 4 Crystal Kelp Frond?",
	done = "You got the kelp! Good job! Wait here.",
}
D["Collecting Kelp"] = D[112]

D[114] = {
	id = 114,
	origTitle = "The Escape",
	title = "Help Maybell!",
	todo = "Take the Invisibility Liquor to Maybell.",
	story = "Here is the drink. Maybell wants it. Take it to Maybell. It is a big secret. Go fast!",
	notDone = "Did you give Maybell the drink?",
	done = "Thank you! I can go now. You helped me!",
}
D["The Escape"] = D[114]

D[1638] = {
	id = 1638,
	origTitle = "A Warrior's Training",
	title = "Go See Harry!",
	todo = "Go see Harry. He is in Stormwind.",
	story = "Hi! I am Lyria. You are a good warrior. Harry can help you more. He is in Stormwind. Go find him.",
	notDone = "",
	done = "Hello! You look strong. Sit with me. I can help you.",
}
D["A Warrior's Training"] = D[1638]

D[2158] = {
	id = 2158,
	origTitle = "Rest and Relaxation",
	title = "Go See Farley!",
	todo = "Go see Farley. He is at the inn.",
	story = "You look tired. Go see Farley. He is at the inn. He can make you rest. Rest is good!",
	notDone = "",
	done = "Hi! I am Farley. Come in and rest. Rest here!",
}
D["Rest and Relaxation"] = D[2158]

D[109] = {
	id = 109,
	origTitle = "Report to Gryan Stoutmantle",
	title = "Go See Gryan!",
	todo = "Go see Gryan. He is at the big tower.",
	story = "Gryan helps the farms. He needs big help. He is far away. Go see Gryan at the tower.",
	notDone = "Did you find Gryan?",
	done = "Thank you for coming! I need help. Will you help me?",
}
D["Report to Gryan Stoutmantle"] = D[109]

D[239] = {
	id = 239,
	origTitle = "Westbrook Garrison Needs Help!",
	title = "Help the Camp!",
	todo = "Go see Rainer. He is at the west camp.",
	story = "The west camp needs help. Bad gnolls are there. Go see Rainer. He knows what to do.",
	notDone = "",
	done = "So Dughan sent you! Good. You can help me!",
}
D["Westbrook Garrison Needs Help!"] = D[239]

D[147] = {
	id = 147,
	origTitle = "Manhunt",
	title = "Find the Collector!",
	todo = "Beat The Collector. Get 1 The Collector's Ring. Go back to Dughan.",
	story = "A thief takes our gold. His name is The Collector. Look at the pumpkin farm. Beat him. Bring me the ring!",
	notDone = "Did you find The Collector?",
	done = "You found him! Good job! Take this prize.",
}
D["Manhunt"] = D[147]

-- ===== Batch 2: farms, mines area, Crystal Lake, Eastvale, warrior chain =====

D[16] = {
	id = 16,
	origTitle = "Give Gerard a Drink",
	title = "Give Gerard a Drink!",
	todo = "Get 1 Refreshing Spring Water. Give it to Gerard.",
	story = "Hi! I am Gerard. The sun is hot. I need a drink. Get spring water for me.",
	notDone = "Do you have the water?",
	done = "Thanks! Come back any time.",
}
D["Give Gerard a Drink"] = D[16]

D[85] = {
	id = 85,
	origTitle = "Lost Necklace",
	title = "Find the Necklace!",
	todo = "Talk to Billy.",
	story = "Hi! I am Bernice. I lost my necklace. Go ask Billy. He took it. Get it back!",
	notDone = "",
	done = "Not me! I did not take it. I know who did.",
}
D["Lost Necklace"] = D[85]

D[86] = {
	id = 86,
	origTitle = "Pie for Billy",
	title = "Make a Pie!",
	todo = "Get 4 Chunk of Boar Meat. Go back to Bernice.",
	story = "Hi! I am Billy. I know about the necklace. Get me a pie first. Bernice can make pies. Boars are by the farms.",
	notDone = "Do you have 4 Chunk of Boar Meat?",
	done = "Boar meat! I can make a pie. Wait here.",
}
D["Pie for Billy"] = D[86]

D[84] = {
	id = 84,
	origTitle = "Back to Billy",
	title = "Give Billy the Pie!",
	todo = "Take the Pork Belly Pie to Billy.",
	story = "Here is the pie. It is hot! Take it to Billy. Do not eat it!",
	notDone = "Do you have the pie?",
	done = "Mm, yum! This pie is the best! I will talk now.",
}
D["Back to Billy"] = D[84]

D[87] = {
	id = 87,
	origTitle = "Goldtooth",
	title = "Beat Goldtooth!",
	todo = "Beat Goldtooth. Get 1 Bernice's Necklace. Go back to Bernice.",
	story = "The necklace is in the mine. A big kobold took it. His name is Goldtooth. He has gold teeth. Beat him!",
	notDone = "Did you find the necklace?",
	done = "Oh, you found it! Thank you, thank you! Take this prize.",
}
D["Goldtooth"] = D[87]

D[88] = {
	id = 88,
	origTitle = "Princess Must Die!",
	title = "Get Princess!",
	todo = "Beat Princess. Get 1 Brass Collar. Go back to Ma.",
	story = "Hi! I am Ma. Princess is a big pig. She eats our food. Look at the pumpkin farm. Beat her!",
	notDone = "Did you get Princess?",
	done = "Thank you! She was so big! Take this prize.",
}
D["Princess Must Die!"] = D[88]

D[106] = {
	id = 106,
	origTitle = "Young Lovers",
	title = "Take the Letter!",
	todo = "Take Maybell's Love Letter to Tommy.",
	story = "Hi! I am Maybell. I like Tommy. I can not go out. Take this letter to Tommy. He is by the river.",
	notDone = "Did you give Tommy the letter?",
	done = "Her letter! I am so happy! I will see her!",
}
D["Young Lovers"] = D[106]

D[111] = {
	id = 111,
	origTitle = "Speak with Gramma",
	title = "Go See Gramma!",
	todo = "Go see Gramma. She is in her house.",
	story = "Hi! I am Tommy. Gramma can help us. She is in her house. It is east of here.",
	notDone = "Did you talk to Gramma?",
	done = "Our farms are not friends. We will help them.",
}
D["Speak with Gramma"] = D[111]

D[107] = {
	id = 107,
	origTitle = "Note to William",
	title = "Take the Note!",
	todo = "Take Gramma Stonefield's Note to William.",
	story = "Hi! I am Gramma. I have a note. Take it to William. He is at the inn. He can help the two farms.",
	notDone = "Did you give William the note?",
	done = "A note from Gramma! I can help them. Wait here.",
}
D["Note to William"] = D[107]

D[11] = {
	id = 11,
	origTitle = "Riverpaw Gnoll Bounty",
	title = "Beat the Gnolls!",
	todo = "Get 8 Painted Gnoll Armband. Go back to Rainer.",
	story = "Hi! I am Rainer. Bad gnolls are in the woods. They are south of here. Beat them and get their armbands.",
	notDone = "Do you have 8 Painted Gnoll Armband?",
	done = "You have been busy! Thank you.",
}
D["Riverpaw Gnoll Bounty"] = D[11]

D[46] = {
	id = 46,
	origTitle = "Bounty on Murlocs",
	title = "Beat the Murlocs!",
	todo = "Get 8 Torn Murloc Fin. Go back to Thomas.",
	story = "Hi! I am Thomas. Murlocs are by the lake. They have a village. Beat them and get their fins.",
	notDone = "Do you have 8 Torn Murloc Fin?",
	done = "You have the fins! Good job! Dughan will be glad.",
}
D["Bounty on Murlocs"] = D[46]

D[52] = {
	id = 52,
	origTitle = "Protect the Frontier",
	title = "Wolves and Bears!",
	todo = "Beat 8 Prowler. Beat 5 Young Forest Bear. Go back to Thomas.",
	story = "Wolves and bears are bad here. They hurt the log camp. Beat them all. Then come back to me.",
	notDone = "Did you beat the wolves and bears?",
	done = "Thank you for the help! Good job!",
}
D["Protect the Frontier"] = D[52]

D[37] = {
	id = 37,
	origTitle = "Find the Lost Guards",
	title = "Find the Guards!",
	todo = "Look for Rolf and Malakai. Go back to Thomas.",
	story = "Two guards are lost. Rolf and Malakai. Look by the river. Find them for me.",
	notDone = "",
	done = "You found them. Thank you. That is sad news.",
}
D["Find the Lost Guards"] = D[37]

D[71] = {
	id = 71,
	origTitle = "Report to Thomas",
	title = "Get the Medallions!",
	todo = "Get 1 Rolf and Malakai's Medallions. Go back to Thomas.",
	story = "The guards are gone. Their medallions are here. Take them to Thomas. He needs to know.",
	notDone = "Did you get the medallions?",
	done = "So the murlocs got them. That is bad. Thank you.",
}
D["Report to Thomas"] = D[71]

D[39] = {
	id = 39,
	origTitle = "Deliver Thomas' Report",
	title = "Go Back to Dughan!",
	todo = "Talk to Dughan.",
	story = "The murlocs are too many. Dughan must know. Go to Dughan in Goldshire. Talk to him about the murlocs.",
	notDone = "",
	done = "This is bad news! I will send more guards. Thank you.",
}
D["Deliver Thomas' Report"] = D[39]

D[83] = {
	id = 83,
	origTitle = "Red Linen Goods",
	title = "Get the Bandanas!",
	todo = "Get 6 Red Linen Bandana. Go back to Sara.",
	story = "Hi! I am Sara. I make clothes here. Bad men have linen bandanas. Get 6 for me. I will make you something.",
	notDone = "Do you have 6 Red Linen Bandana?",
	done = "Good bandanas! Here is something for you.",
}
D["Red Linen Goods"] = D[83]

D[5545] = {
	id = 5545,
	origTitle = "A Bundle of Trouble",
	title = "Get the Wood!",
	todo = "Get 8 Bundle of Wood. Go back to Raelen.",
	story = "Hi! I am Raelen. The wood is out there. Wolves and bears scared my men. Get 8 bundles for me.",
	notDone = "Do you have 8 Bundle of Wood?",
	done = "You got the wood! Thank you!",
}
D["A Bundle of Trouble"] = D[5545]

D[1639] = {
	id = 1639,
	origTitle = "Bartleby the Drunk",
	title = "Get the Mug!",
	todo = "Talk to Bartleby.",
	story = "Hi! I am Harry. Bartleby has a mug. I want it for class. Go talk to Bartleby. He will not give it easy.",
	notDone = "",
	done = "You want my mug? Ha! You are funny. You will have to beat me first!",
}
D["Bartleby the Drunk"] = D[1639]

D[1640] = {
	id = 1640,
	origTitle = "Beat Bartleby",
	title = "Beat Bartleby!",
	todo = "Beat Bartleby. Then talk to him.",
	story = "Hi! I am Bartleby. You want my mug? Then beat me! I am not easy. Show me your moves!",
	notDone = "",
	done = "You are strong! You beat me!",
}
D["Beat Bartleby"] = D[1640]

D[1665] = {
	id = 1665,
	origTitle = "Bartleby's Mug",
	title = "Take the Mug!",
	todo = "Take Bartleby's Mug to Harry.",
	story = "Here is my mug. I am done with it. Take it to Harry. Good luck!",
	notDone = "Did you get Bartleby's mug?",
	done = "There it is! Well done! I will help you now.",
}
D["Bartleby's Mug"] = D[1665]

D[1666] = {
	id = 1666,
	origTitle = "Marshal Haggard",
	title = "Go See Haggard!",
	todo = "Go see Haggard. He is at the log camp.",
	story = "You did good. Haggard is an old hero. He has good things. Go see Haggard. He can help you.",
	notDone = "",
	done = "Hi! I am Haggard. You are not a gnoll! Come closer. I have something for you.",
}
D["Marshal Haggard"] = D[1666]
