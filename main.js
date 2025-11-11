// fetch("http://www.7timer.info/bin/api.pl?lon=113.17&lat=23.09&product=astro&output=xml")
const params = new URLSearchParams({ name: "michalel" });
const options = {
	headers: { "Content-Type": "application/json" },
	method: "GET",
};

fetch(`https://api.agify.io?${params.toString()}`, options)
	.then((res) => {
    console.log("Entered first then")
    return res.json();
  }).then(data => {
    console.log("Entered second then")
    console.log({data})
  })
	.catch((e) => console.error(`This is the error\n${e}`))
	.catch((e) => "Outermost catch block");
